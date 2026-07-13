from dataclasses import dataclass, replace
from datetime import datetime

from .artifacts import ArtifactRef
from .enums import RiskClass, Role, TaskStatus
from .errors import InvalidTransitionError, ValidationError


@dataclass(frozen=True)
class TaskSource:
    kind: str
    reference: str

    def __post_init__(self) -> None:
        if not self.kind or not self.reference:
            raise ValidationError("task source kind and reference are required")


@dataclass(frozen=True)
class RepositoryTarget:
    name: str
    revision: str

    def __post_init__(self) -> None:
        if not self.name or not self.revision or self.name.startswith(("/", "\\")):
            raise ValidationError("repository target must be a provider-neutral name and revision")


@dataclass(frozen=True)
class RiskAssessment:
    proposed: RiskClass
    validated: RiskClass
    effective: RiskClass
    escalation_reason: str | None = None
    governance_revision: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, RiskClass) for value in (self.proposed, self.validated, self.effective)):
            raise ValidationError("risk values must be R0 through R3")
        if self.effective.value < self.validated.value or self.effective.value < self.proposed.value:
            raise ValidationError("effective risk cannot be lower than proposed or validated risk")
        if self.effective != self.proposed and not self.escalation_reason:
            raise ValidationError("risk escalation requires a reason")


_TRANSITIONS: dict[TaskStatus, tuple[TaskStatus, Role]] = {
    TaskStatus.RECEIVED: (TaskStatus.VALIDATED, Role.VALIDATOR),
    TaskStatus.VALIDATED: (TaskStatus.PLANNED, Role.PLANNER),
    TaskStatus.PLANNED: (TaskStatus.DESIGN_REVIEW, Role.DESIGN_REVIEWER),
    TaskStatus.DESIGN_REVIEW: (TaskStatus.IMPLEMENTING, Role.IMPLEMENTER),
    TaskStatus.IMPLEMENTING: (TaskStatus.PREFLIGHT, Role.IMPLEMENTER),
    TaskStatus.PREFLIGHT: (TaskStatus.PATCH_REVIEW, Role.PATCH_REVIEWER),
    TaskStatus.PATCH_REVIEW: (TaskStatus.READY_TO_PUBLISH, Role.PATCH_REVIEWER),
}


@dataclass(frozen=True)
class Task:
    task_id: str
    source: TaskSource
    repository: RepositoryTarget
    risk: RiskAssessment
    created_at: datetime
    status: TaskStatus = TaskStatus.RECEIVED
    version: int = 0
    rework_round: int = 0
    artifacts: tuple[ArtifactRef, ...] = ()

    @classmethod
    def create(cls, task_id: str, source: TaskSource, repository: RepositoryTarget, risk: RiskAssessment, created_at: datetime) -> "Task":
        if not task_id:
            raise ValidationError("task id is required")
        return cls(task_id=task_id, source=source, repository=repository, risk=risk, created_at=created_at)

    def transition(self, target: TaskStatus, actor: Role, occurred_at: datetime) -> "Task":
        if self.status in (TaskStatus.FAILED, TaskStatus.COMPLETED):
            raise InvalidTransitionError("terminal tasks cannot transition")
        if target in (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.COMPLETED):
            raise InvalidTransitionError("terminal and blocked states require explicit operations")
        if self.status is TaskStatus.DESIGN_REVIEW and target is TaskStatus.PROPOSAL_READY:
            if actor is Role.DESIGN_REVIEWER and self.risk.effective is RiskClass.R3:
                return replace(self, status=target, version=self.version + 1)
            raise InvalidTransitionError("only R3 design review can produce a proposal")
        if self.status is TaskStatus.DESIGN_REVIEW and target is TaskStatus.IMPLEMENTING and self.risk.effective is RiskClass.R3:
            return replace(self, status=TaskStatus.BLOCKED, version=self.version + 1)
        allowed = _TRANSITIONS.get(self.status)
        if allowed != (target, actor):
            raise InvalidTransitionError(f"{self.status.value} cannot transition to {target.value} by {actor.value}")
        if target in (TaskStatus.IMPLEMENTING, TaskStatus.READY_TO_PUBLISH) and not self.risk.governance_revision:
            return replace(self, status=TaskStatus.BLOCKED, version=self.version + 1)
        return replace(self, status=target, version=self.version + 1)

    def block(self) -> "Task":
        if self.status in (TaskStatus.FAILED, TaskStatus.COMPLETED):
            raise InvalidTransitionError("terminal tasks cannot be blocked")
        return replace(self, status=TaskStatus.BLOCKED, version=self.version + 1)

    def fail(self, actor: Role) -> "Task":
        if self.status in (TaskStatus.FAILED, TaskStatus.COMPLETED) or actor is not Role.SYSTEM:
            raise InvalidTransitionError("only the system can fail a non-terminal task")
        return replace(self, status=TaskStatus.FAILED, version=self.version + 1)

    def request_rework(self, actor: Role) -> "Task":
        if actor not in (Role.DESIGN_REVIEWER, Role.PATCH_REVIEWER):
            raise InvalidTransitionError("only a reviewer can request rework")
        next_round = self.rework_round + 1
        status = TaskStatus.BLOCKED if next_round >= 3 else TaskStatus.REWORK_REQUIRED
        return replace(self, status=status, rework_round=next_round, version=self.version + 1)

    def restart_rework(self, actor: Role) -> "Task":
        if self.status is not TaskStatus.REWORK_REQUIRED or actor is not Role.PLANNER:
            raise InvalidTransitionError("only planner can restart an active rework round")
        return replace(self, status=TaskStatus.PLANNED, version=self.version + 1)

    def unblock(self, actor: Role) -> "Task":
        if self.status is not TaskStatus.BLOCKED or actor is not Role.VALIDATOR:
            raise InvalidTransitionError("only validator can explicitly unblock a task")
        return replace(self, status=TaskStatus.VALIDATED, version=self.version + 1)

    def attach_artifact(self, artifact: ArtifactRef) -> "Task":
        if artifact in self.artifacts:
            return self
        return replace(self, artifacts=(*self.artifacts, artifact), version=self.version + 1)
