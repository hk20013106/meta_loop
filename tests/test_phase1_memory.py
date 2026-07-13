from datetime import UTC, datetime, timedelta

import pytest

from meta_loop.domain.enums import RiskClass
from meta_loop.domain.errors import OptimisticConflictError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import FixedClock, InMemoryTaskQueue, InMemoryTaskRepository, SequentialIdGenerator


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def task() -> Task:
    return Task.create("task-1", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)


def test_repository_uses_optimistic_versions_and_test_utilities_are_deterministic():
    repository = InMemoryTaskRepository()
    repository.save(task(), expected_version=0)

    with pytest.raises(OptimisticConflictError):
        repository.save(task(), expected_version=1)

    assert FixedClock(NOW).now() == NOW
    assert SequentialIdGenerator("event").new() == "event-1"


def test_queue_claim_is_exclusive_and_expired_lease_can_be_recovered():
    queue = InMemoryTaskQueue()
    queue.enqueue("task-1", priority=1, available_at=NOW, idempotency_key="enqueue-1")
    first = queue.claim("worker-a", NOW, timedelta(seconds=300))

    assert first is not None
    assert queue.claim("worker-b", NOW, timedelta(seconds=300)) is None
    recovered = queue.recover_expired(NOW + timedelta(seconds=301))

    assert len(recovered) == 1 and not recovered[0].terminal
    assert queue.claim("worker-b", NOW + timedelta(seconds=301), timedelta(seconds=300)).task_id == "task-1"
