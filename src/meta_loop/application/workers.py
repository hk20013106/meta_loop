"""Role-constrained worker orchestration over the Phase 4 runner protocol."""

from meta_loop.application.artifacts import ArtifactRegistrationService, ArtifactRequest
from meta_loop.application.models import RunnerSessionOutcome, WorkerResult, WorkerResultReceipt
from meta_loop.application.ports import UnitOfWork
from meta_loop.application.services import TaskEventService
from meta_loop.domain.enums import EventType, Role
from meta_loop.domain.errors import IdempotencyConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError
from meta_loop.domain.events import Event


class WorkerOrchestrationService:
    def run(self, uow: UnitOfWork, runner, session_id: str) -> RunnerSessionOutcome:
        if uow.fuse.get().engaged:
            raise ValidationError("fuse is engaged")
        record = uow.runner_sessions.get(session_id)
        if record is None:
            raise TaskNotFoundError("runner session does not exist")
        if record.outcome is not None:
            return record.outcome
        outcome = runner.run(record.request)
        if outcome.session_id != session_id:
            raise ValidationError("runner returned a mismatched session outcome")
        uow.runner_sessions.set_outcome(outcome)
        return outcome


class ResultPublicationService:
    def __init__(self, artifacts: ArtifactRegistrationService, ids, clock) -> None:
        self._artifacts, self._ids, self._clock = artifacts, ids, clock

    def publish(self, uow: UnitOfWork, result: WorkerResult, chunks, artifact_request: ArtifactRequest, now) -> WorkerResultReceipt:
        existing = uow.worker_results.get(result.result_id)
        if existing is not None:
            if existing.canonical() != result.canonical():
                raise IdempotencyConflictError("worker result id has conflicting content")
            return WorkerResultReceipt(result.result_id, False)
        session = uow.runner_sessions.get(result.session_id)
        if session is None or session.outcome is None or session.outcome.state.value != "succeeded":
            raise ValidationError("worker result requires a successful runner session")
        if session.request.task_id != result.task_id or session.request.role is not result.worker.role:
            raise ValidationError("worker result does not match its runner session")
        task = uow.tasks.get(result.task_id)
        if task is None:
            raise TaskNotFoundError("worker result task does not exist")
        if task.version != result.expected_task_version or len(uow.events.read(task.task_id)) != result.expected_sequence:
            raise ValidationError("worker result has stale task or event version")
        reference = self._artifacts.register(uow, task.task_id, chunks, artifact_request, result.expected_task_version, result.expected_sequence)
        updated = uow.tasks.get(task.task_id)
        event = Event(self._ids.new(), task.task_id, EventType.TASK_TRANSITIONED, self._clock.now(), result.worker.role, {"result_id": result.result_id, "summary": result.summary, "artifact": reference.to_dict()}, 1, result.session_id, result.session_id, result.expected_sequence + 2)
        TaskEventService().transition(uow, task.task_id, result.target, result.worker.role, now, event, updated.version, result.expected_sequence + 1, {"result_id": result.result_id, "artifact": reference.to_dict()})
        uow.queue.complete(task.task_id, result.worker.worker_id, now)
        return uow.worker_results.record_once(result)

    def cancel(self, uow: UnitOfWork, task_id: str, worker_id: str, now, governance, revision: str | None, expected_sequence: int) -> None:
        if governance is None or not revision:
            raise UnsupportedGovernanceError("worker cancellation requires governance authorization")
        try:
            governance.read_revision(revision)
            if not governance.authorize(revision, "cancel_execution"):
                raise UnsupportedGovernanceError("worker cancellation is not authorized")
        except UnsupportedGovernanceError:
            raise
        except Exception as error:
            raise UnsupportedGovernanceError("worker cancellation is not authorized") from error
        task = uow.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("worker cancellation task does not exist")
        blocked = task.block()
        uow.tasks.update(blocked, task.version)
        uow.events.append(Event(self._ids.new(), task_id, EventType.TASK_BLOCKED, self._clock.now(), Role.SYSTEM, {"reason": "cancel_execution"}, 1, None, task_id, expected_sequence + 1), expected_sequence)
        uow.queue.complete(task_id, worker_id, now)
