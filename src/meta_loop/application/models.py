from dataclasses import dataclass
from datetime import datetime


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
