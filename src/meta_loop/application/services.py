"""Application-level atomic use cases; domain code remains infrastructure-free."""

from collections.abc import Mapping

from meta_loop.application.ports import UnitOfWork
from meta_loop.domain.enums import Role, TaskStatus
from meta_loop.domain.errors import TaskNotFoundError, ValidationError
from meta_loop.domain.events import Event
from meta_loop.domain.task import Task


class TaskEventService:
    def transition(self, uow: UnitOfWork, task_id: str, target: TaskStatus, actor: Role, occurred_at, event: Event, expected_version: int, expected_sequence: int, evidence: Mapping[str, object], governance=None) -> Task:
        task = uow.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("task does not exist")
        if not evidence:
            raise ValidationError("transition evidence is required")
        if target in (TaskStatus.IMPLEMENTING, TaskStatus.READY_TO_PUBLISH):
            revision = task.risk.governance_revision
            authorized = False
            if governance is not None and revision:
                try:
                    governance.read_revision(revision)
                    authorized = governance.authorize(revision, "implement" if target is TaskStatus.IMPLEMENTING else "publish")
                except Exception:
                    authorized = False
            task = task.transition(target, actor, occurred_at) if authorized else task.block()
        else:
            task = task.transition(target, actor, occurred_at)
        uow.tasks.update(task, expected_version)
        uow.events.append(event, expected_sequence)
        return task


class TaskQueueService:
    def fail(self, uow: UnitOfWork, task_id: str, worker_id: str, now, error: str, event: Event, expected_sequence: int):
        task = uow.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("task does not exist")
        result = uow.queue.fail(task_id, worker_id, now, error)
        if result.terminal:
            uow.tasks.update(task.fail(Role.SYSTEM), task.version)
        uow.events.append(event, expected_sequence)
        return result

    def recover_expired(self, uow: UnitOfWork, now, terminal_events: Mapping[str, tuple[Event, int]]):
        results = uow.queue.recover_expired(now)
        for result in results:
            if result.terminal:
                task = uow.tasks.get(result.task_id)
                if task is None:
                    raise TaskNotFoundError("task does not exist")
                event, expected_sequence = terminal_events[result.task_id]
                uow.tasks.update(task.fail(Role.SYSTEM), task.version)
                uow.events.append(event, expected_sequence)
        return results
