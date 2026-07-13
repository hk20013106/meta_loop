"""Phase 2 application controller; it only coordinates established ports."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from meta_loop.application.models import ControlEvent, FuseState, IntakeRecord
from meta_loop.application.ports import Clock, IdGenerator, UnitOfWork
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import CreateConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError
from meta_loop.domain.events import Event
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource


@dataclass(frozen=True)
class IntakeRequest:
    request_id: str
    source_kind: str
    source_reference: str
    repository_name: str
    repository_revision: str
    proposed_risk: RiskClass
    validated_risk: RiskClass
    effective_risk: RiskClass
    governance_revision: str | None = None
    enqueue: bool = False
    priority: int = 0

    def canonical(self) -> str:
        return json.dumps({"schema_version": 1, "request_id": self.request_id,
                           "source": {"kind": self.source_kind, "reference": self.source_reference},
                           "repository": {"name": self.repository_name, "revision": self.repository_revision},
                           "risk": {"proposed": self.proposed_risk.value, "validated": self.validated_risk.value,
                                    "effective": self.effective_risk.value, "governance_revision": self.governance_revision},
                           "enqueue": self.enqueue, "priority": self.priority},
                          sort_keys=True, separators=(",", ":"))


class TaskIntakeService:
    def __init__(self, clock: Clock, ids: IdGenerator) -> None:
        self._clock, self._ids = clock, ids

    def start(self, uow: UnitOfWork, request: IntakeRequest) -> Task:
        if not request.request_id:
            raise ValidationError("request_id is required")
        if uow.fuse.get().engaged:
            raise ValidationError("fuse is engaged")
        canonical = request.canonical()
        record = uow.intake_ledger.get(request.request_id)
        if record is not None:
            if record.canonical_request != canonical:
                raise CreateConflictError("request_id has conflicting intake")
            existing = uow.tasks.get(record.task_id)
            if existing is None:
                raise TaskNotFoundError("intake ledger references a missing task")
            return existing
        now = self._clock.now()
        task = Task.create(request.request_id, TaskSource(request.source_kind, request.source_reference), RepositoryTarget(request.repository_name, request.repository_revision), RiskAssessment(request.proposed_risk, request.validated_risk, request.effective_risk, governance_revision=request.governance_revision), now)
        event = Event(self._ids.new(), task.task_id, EventType.TASK_RECEIVED, now, Role.SYSTEM, {"request_id": request.request_id}, 1, None, request.request_id, 1)
        uow.intake_ledger.create(IntakeRecord(request.request_id, canonical, task.task_id))
        uow.tasks.create(task)
        uow.events.append(event, 0)
        if request.enqueue:
            uow.queue.enqueue(task.task_id, request.priority, now, request.request_id)
        return task


class TaskQueryService:
    def status(self, uow: UnitOfWork, task_id: str) -> tuple[Task, tuple[Event, ...]]:
        task = uow.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("task does not exist")
        return task, uow.events.read(task_id)

    def tasks(self, uow: UnitOfWork, status: TaskStatus | None = None, risk: RiskClass | None = None) -> tuple[Task, ...]:
        values = (task for task in uow.tasks.list() if (status is None or task.status is status) and (risk is None or task.risk.effective is risk))
        return tuple(sorted(values, key=lambda task: (task.created_at, task.task_id)))


class QueueControlService:
    def claim(self, uow: UnitOfWork, worker_id: str, now: datetime, lease: timedelta):
        if uow.fuse.get().engaged:
            raise ValidationError("fuse is engaged")
        return uow.queue.claim(worker_id, now, lease)


class FuseService:
    """Fail-closed policy helper; persistence is supplied by the caller's store."""

    def engage(self, uow: UnitOfWork, now: datetime) -> FuseState:
        state = uow.fuse.set(FuseState(True, now))
        uow.control_events.append(ControlEvent("fuse_engaged", now))
        return state

    def release(self, uow: UnitOfWork, now: datetime, governance, revision: str | None) -> FuseState:
        if governance is None or not revision:
            raise UnsupportedGovernanceError("fuse release requires governance authorization")
        try:
            governance.read_revision(revision)
            if not governance.authorize(revision, "fuse_release"):
                raise UnsupportedGovernanceError("fuse release is not authorized")
        except UnsupportedGovernanceError:
            raise
        except Exception as error:
            raise UnsupportedGovernanceError("fuse release is not authorized") from error
        state = uow.fuse.set(FuseState(False, now, revision))
        uow.control_events.append(ControlEvent("fuse_released", now, revision))
        return state
