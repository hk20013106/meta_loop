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
