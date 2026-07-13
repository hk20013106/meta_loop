from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json

from meta_loop.domain.artifacts import ArtifactRef
from meta_loop.domain.enums import Role
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
