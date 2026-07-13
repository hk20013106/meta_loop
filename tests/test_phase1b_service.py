from datetime import UTC, datetime

import pytest

from meta_loop.application.services import TaskEventService, TaskQueueService
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import LeaseLostError
from meta_loop.domain.events import Event
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def test_service_persists_task_transition_and_event_in_one_uow():
    uow = InMemoryUnitOfWork()
    original = Task.create("task-1", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)
    with uow:
        uow.tasks.create(original)
        uow.commit()

    event = Event("event-1", "task-1", EventType.TASK_TRANSITIONED, NOW, Role.VALIDATOR, {}, 1, None, "c", 1)
    with uow:
        TaskEventService().transition(uow, "task-1", TaskStatus.VALIDATED, Role.VALIDATOR, NOW, event, expected_version=0, expected_sequence=0, evidence={"validation": "ok"})
        uow.commit()

    assert uow.tasks.get("task-1").status is TaskStatus.VALIDATED
    assert uow.events.read("task-1") == (event,)


def test_service_blocks_execution_without_verifiable_governance():
    uow = InMemoryUnitOfWork()
    original = Task.create("task-governance", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)
    with uow:
        uow.tasks.create(original)
        uow.commit()

    event = Event("event-governance", original.task_id, EventType.TASK_BLOCKED, NOW, Role.SYSTEM, {}, 1, None, "c", 1)
    with uow:
        TaskEventService().transition(uow, original.task_id, TaskStatus.IMPLEMENTING, Role.IMPLEMENTER, NOW, event, 0, 0, evidence={"design": "approved"})
        uow.commit()

    assert uow.tasks.get(original.task_id).status is TaskStatus.BLOCKED


def test_terminal_queue_failure_marks_task_failed_and_appends_event_atomically():
    uow = InMemoryUnitOfWork()
    original = Task.create("task-2", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)
    with uow:
        uow.tasks.create(original)
        uow.queue.enqueue(original.task_id, 0, NOW, "enqueue-1")
        uow.queue.claim("worker", NOW, __import__("datetime").timedelta(seconds=30))
        uow.queue._records[original.task_id] = __import__("dataclasses").replace(uow.queue._records[original.task_id], attempt=3)
        result = TaskQueueService().fail(uow, original.task_id, "worker", NOW, "failed", Event("event-2", original.task_id, EventType.TASK_TRANSITIONED, NOW, Role.SYSTEM, {}, 1, None, "c", 1), 0)
        assert result.terminal
        uow.commit()

    assert uow.tasks.get(original.task_id).status is TaskStatus.FAILED
    assert len(uow.events.read(original.task_id)) == 1


def test_completion_rejects_an_expired_lease():
    uow = InMemoryUnitOfWork()
    original = Task.create("task-expired", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)
    with uow:
        uow.tasks.create(original)
        uow.queue.enqueue(original.task_id, 0, NOW, "enqueue-1")
        uow.queue.claim("worker", NOW, __import__("datetime").timedelta(seconds=1))
        with pytest.raises(LeaseLostError):
            uow.queue.complete(original.task_id, "worker", NOW + __import__("datetime").timedelta(seconds=2))


def test_expired_final_attempt_fails_task_and_appends_event_in_one_uow():
    uow = InMemoryUnitOfWork()
    original = Task.create("task-recovery", TaskSource("manual", "r"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)
    with uow:
        uow.tasks.create(original)
        uow.queue.enqueue(original.task_id, 0, NOW, "enqueue-1")
        uow.queue.claim("worker", NOW, __import__("datetime").timedelta(seconds=1))
        uow.queue._records[original.task_id] = __import__("dataclasses").replace(uow.queue._records[original.task_id], attempt=3)
        event = Event("event-recovery", original.task_id, EventType.TASK_TRANSITIONED, NOW, Role.SYSTEM, {}, 1, None, "c", 1)
        result = TaskQueueService().recover_expired(uow, NOW + __import__("datetime").timedelta(seconds=2), {original.task_id: (event, 0)})
        assert result[0].terminal
        uow.commit()

    assert uow.tasks.get(original.task_id).status is TaskStatus.FAILED
    assert uow.events.read(original.task_id) == (event,)
