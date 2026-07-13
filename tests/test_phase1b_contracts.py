from datetime import UTC, datetime

import pytest

from meta_loop.application.models import Lease, QueueCompletionResult, QueueFailureResult
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import EventType, RiskClass, Role
from meta_loop.domain.errors import IdempotencyConflictError, LeaseLostError, UnsupportedSchemaVersionError
from meta_loop.domain.events import Event
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork
from meta_loop.serialization.codecs import task_from_dict, task_to_dict


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def task() -> Task:
    return Task(
        task_id="task-1", source=TaskSource("manual", "request-1"),
        repository=RepositoryTarget("public/repo", "main"),
        risk=RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1), created_at=NOW,
        artifacts=(ArtifactRef(ArtifactDigest("a" * 64), "plan.json", "application/json"),),
    )


def test_task_codec_round_trips_all_phase1_fields_and_rejects_unknown_version():
    encoded = task_to_dict(task())

    assert task_from_dict(encoded) == task()
    encoded["schema_version"] = 2
    with pytest.raises(UnsupportedSchemaVersionError):
        task_from_dict(encoded)


def test_queue_results_are_provider_neutral_and_explicit():
    lease = Lease("task-1", "worker-1", NOW, 1)

    assert QueueCompletionResult("task-1", terminal=True).terminal
    assert QueueFailureResult("task-1", retry_at=None, terminal=True).terminal
    assert lease.worker_id == "worker-1"


def test_memory_unit_of_work_rolls_back_uncommitted_changes():
    uow = InMemoryUnitOfWork()

    with uow:
        uow.tasks.create(task())

    assert uow.tasks.get("task-1") is None


def test_memory_unit_of_work_commits_queue_and_task_together():
    uow = InMemoryUnitOfWork()
    with uow:
        uow.tasks.create(task())
        uow.queue.enqueue("task-1", 0, NOW, "enqueue-1")
        uow.commit()

    with uow:
        assert uow.queue.claim("worker-1", NOW, __import__("datetime").timedelta(seconds=30)) is not None


def test_memory_event_id_conflict_uses_explicit_error():
    event = Event("event-1", "task-1", EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {}, 1, None, "c", 1)
    uow = InMemoryUnitOfWork()
    with uow:
        uow.events.append(event, expected_sequence=0)
        with pytest.raises(IdempotencyConflictError):
            uow.events.append(Event("event-1", "task-1", EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {"changed": True}, 1, None, "c", 2), expected_sequence=1)


def test_memory_queue_rejects_non_owner_with_lease_lost_error():
    uow = InMemoryUnitOfWork()
    with uow:
        uow.queue.enqueue("task-1", 0, NOW, "enqueue-1")
        uow.queue.claim("owner", NOW, __import__("datetime").timedelta(seconds=30))
        with pytest.raises(LeaseLostError):
            uow.queue.complete("task-1", "other", NOW)
