"""Provider-neutral runner protocol; no adapter invokes a process in Phase 4."""

from meta_loop.application.models import RunnerSessionOutcome, RunnerSessionReceipt, RunnerSessionRequest, RunnerSessionState
from meta_loop.application.ports import Clock, IdGenerator, UnitOfWork
from meta_loop.domain.enums import EventType, RiskClass, Role
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError, TaskNotFoundError, ValidationError
from meta_loop.domain.events import Event


class FakeRunner:
    """Deterministic test double. It has no environment, filesystem, or task access."""

    def run(self, request: RunnerSessionRequest) -> RunnerSessionOutcome:
        return RunnerSessionOutcome(request.session_id, RunnerSessionState.SUCCEEDED, "accepted by fake runner")


class RunnerSessionService:
    def __init__(self, clock: Clock, ids: IdGenerator) -> None:
        self._clock, self._ids = clock, ids

    def request(self, uow: UnitOfWork, request: RunnerSessionRequest) -> RunnerSessionReceipt:
        task = uow.tasks.get(request.task_id)
        if task is None:
            raise TaskNotFoundError("runner session task does not exist")
        existing = uow.runner_sessions.get(request.session_id)
        if existing is not None:
            if existing.request.canonical() != request.canonical():
                raise IdempotencyConflictError("runner session id has conflicting request")
            return RunnerSessionReceipt(request.session_id, existing.state, False)
        if uow.fuse.get().engaged:
            raise ValidationError("fuse is engaged")
        if task.risk.effective is RiskClass.R3 and request.role is Role.IMPLEMENTER:
            raise ValidationError("R3 tasks cannot request an implementer session")
        if task.version != request.expected_task_version:
            raise OptimisticConflictError("runner session task version does not match")
        if len(uow.events.read(task.task_id)) != request.expected_sequence:
            raise OptimisticConflictError("runner session event sequence does not match")
        if any(reference not in task.artifacts for reference in request.input_artifacts):
            raise ValidationError("runner session inputs must be task artifact references")
        receipt = uow.runner_sessions.record_once(request)
        if receipt.created:
            event = Event(
                self._ids.new(), task.task_id, EventType.RUNNER_SESSION_REQUESTED,
                self._clock.now(), Role.SYSTEM,
                {"session_id": request.session_id, "role": request.role.value,
                 "input_artifacts": [reference.to_dict() for reference in request.input_artifacts]},
                1, None, request.correlation_id, request.expected_sequence + 1,
            )
            uow.events.append(event, request.expected_sequence)
        return receipt
