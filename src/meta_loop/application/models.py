from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json

from meta_loop.domain.artifacts import ArtifactRef
from meta_loop.domain.enums import Role, TaskStatus
from meta_loop.domain.errors import ValidationError


@dataclass(frozen=True)
class Lease:
    task_id: str
    worker_id: str
    expires_at: datetime
    attempt: int


@dataclass(frozen=True)
class QueueEnqueueResult:
    task_id: str
    created: bool


@dataclass(frozen=True)
class QueueCompletionResult:
    task_id: str
    terminal: bool


@dataclass(frozen=True)
class QueueFailureResult:
    task_id: str
    retry_at: datetime | None
    terminal: bool


@dataclass(frozen=True)
class FuseState:
    engaged: bool
    changed_at: datetime
    governance_revision: str | None = None


@dataclass(frozen=True)
class IntakeRecord:
    request_id: str
    canonical_request: str
    task_id: str


@dataclass(frozen=True)
class IssueIngestionRecord:
    source_key: str
    canonical_request: str
    task_id: str
    trigger_event_id: str


@dataclass(frozen=True)
class IssueIngestionReceipt:
    task_id: str
    created: bool


@dataclass(frozen=True)
class ControlEvent:
    action: str
    occurred_at: datetime
    governance_revision: str | None = None


@dataclass(frozen=True)
class CatalogedArtifact:
    reference: ArtifactRef
    classification: str
    source_kind: str
    size_bytes: int


class RunnerSessionState(str, Enum):
    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunnerSessionRequest:
    session_id: str
    task_id: str
    role: Role
    expected_task_version: int
    expected_sequence: int
    input_artifacts: tuple[ArtifactRef, ...]
    timeout_seconds: int
    correlation_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.session_id or not self.task_id or not self.correlation_id:
            raise ValidationError("runner session identifiers and schema version are required")
        if self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("runner role is not supported")
        if self.expected_task_version < 0 or self.expected_sequence < 0 or not 1 <= self.timeout_seconds <= 3600:
            raise ValidationError("runner session versions and timeout are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "expected_task_version": self.expected_task_version,
            "expected_sequence": self.expected_sequence,
            "input_artifacts": [artifact.to_dict() for artifact in self.input_artifacts],
            "timeout_seconds": self.timeout_seconds,
            "correlation_id": self.correlation_id,
        }

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunnerSessionOutcome:
    session_id: str
    state: RunnerSessionState
    summary: str

    def __post_init__(self) -> None:
        if not self.session_id or self.state is RunnerSessionState.REQUESTED or not self.summary or len(self.summary) > 512:
            raise ValidationError("runner outcome is invalid")


@dataclass(frozen=True)
class RunnerSessionRecord:
    request: RunnerSessionRequest
    state: RunnerSessionState = RunnerSessionState.REQUESTED
    outcome: RunnerSessionOutcome | None = None


@dataclass(frozen=True)
class RunnerSessionReceipt:
    session_id: str
    state: RunnerSessionState
    created: bool


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.worker_id or self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("worker identity is not authorized for a worker role")


@dataclass(frozen=True)
class WorkerResult:
    result_id: str
    session_id: str
    task_id: str
    worker: WorkerIdentity
    target: TaskStatus
    summary: str
    expected_task_version: int
    expected_sequence: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.result_id or not self.session_id or not self.task_id or not self.summary or len(self.summary) > 512:
            raise ValidationError("worker result is invalid")
        if not isinstance(self.target, TaskStatus) or self.expected_task_version < 0 or self.expected_sequence < 0 or self.schema_version != 1:
            raise ValidationError("worker result version is invalid")

    def canonical(self) -> str:
        return json.dumps({"schema_version": 1, "result_id": self.result_id, "session_id": self.session_id, "task_id": self.task_id, "worker_id": self.worker.worker_id, "role": self.worker.role.value, "target": self.target.value, "summary": self.summary, "expected_task_version": self.expected_task_version, "expected_sequence": self.expected_sequence}, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkerResultReceipt:
    result_id: str
    created: bool


class WorkspacePurpose(str, Enum):
    PLANNING = "planning"
    REVIEW = "review"
    PROPOSAL = "proposal"
    IMPLEMENTATION = "implementation"


class WorkspaceState(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"


@dataclass(frozen=True)
class WorkspaceRequest:
    allocation_id: str
    task_id: str
    role: Role
    purpose: WorkspacePurpose
    source_revision: str
    expected_task_version: int
    expected_sequence: int
    correlation_id: str
    governance_revision: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (not self.allocation_id or not self.task_id or not self.correlation_id
                or self.schema_version != 1 or self.expected_task_version < 0
                or self.expected_sequence < 0):
            raise ValidationError("workspace request identifiers and versions are invalid")
        if self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("workspace role is not supported")
        if not isinstance(self.purpose, WorkspacePurpose):
            raise ValidationError("workspace purpose is invalid")
        if len(self.source_revision) < 40 or any(character not in "0123456789abcdef" for character in self.source_revision.lower()):
            raise ValidationError("workspace source revision must be a full hexadecimal Git revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "allocation_id": self.allocation_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "purpose": self.purpose.value,
            "source_revision": self.source_revision,
            "expected_task_version": self.expected_task_version,
            "expected_sequence": self.expected_sequence,
            "correlation_id": self.correlation_id,
            "governance_revision": self.governance_revision,
        }

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkspaceRecord:
    request: WorkspaceRequest
    read_only: bool
    repository_name: str
    state: WorkspaceState = WorkspaceState.ACTIVE

    @property
    def workspace_id(self) -> str:
        return self.request.allocation_id


@dataclass(frozen=True)
class WorkspaceReceipt:
    allocation_id: str
    state: WorkspaceState
    created: bool
