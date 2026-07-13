from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from meta_loop.application.models import ControlEvent, FuseState, IntakeRecord, Lease, QueueCompletionResult, QueueEnqueueResult, QueueFailureResult
from meta_loop.domain.errors import CreateConflictError, IdempotencyConflictError, LeaseLostError, OptimisticConflictError, SequenceConflictError, ValidationError
from meta_loop.domain.events import Event
from meta_loop.domain.task import Task


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class SequentialIdGenerator:
    def __init__(self, prefix: str) -> None:
        self._prefix, self._number = prefix, 0

    def new(self) -> str:
        self._number += 1
        return f"{self._prefix}-{self._number}"


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def create(self, task: Task) -> Task:
        if task.version != 0:
            raise OptimisticConflictError("new task must start at version zero")
        if task.task_id in self._tasks:
            raise CreateConflictError("task already exists")
        self._tasks[task.task_id] = task
        return task

    def update(self, task: Task, expected_version: int) -> Task:
        current = self._tasks.get(task.task_id)
        if current is None or current.version != expected_version or task.version != expected_version + 1:
            raise OptimisticConflictError("task version does not match")
        self._tasks[task.task_id] = task
        return task

    def save(self, task: Task, expected_version: int) -> Task:
        return self.create(task) if task.task_id not in self._tasks else self.update(task, expected_version)

    def list(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}

    def append(self, event: Event, expected_sequence: int) -> Event:
        existing = self._event_ids.get(event.event_id)
        if existing is not None:
            if existing == event:
                return existing
            raise IdempotencyConflictError("event id cannot be reused with different content")
        history = self._events.setdefault(event.task_id, [])
        if len(history) != expected_sequence or event.sequence != expected_sequence + 1:
            raise SequenceConflictError("event sequence conflict")
        history.append(event)
        self._event_ids[event.event_id] = event
        return event

    def read(self, task_id: str) -> tuple[Event, ...]:
        return tuple(self._events.get(task_id, ()))


@dataclass(frozen=True)
class _QueueRecord:
    task_id: str
    priority: int
    available_at: datetime
    idempotency_key: str
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    attempt: int = 0


class InMemoryTaskQueue:
    def __init__(self, records: dict[str, _QueueRecord] | None = None) -> None:
        self._records = {} if records is None else records

    def enqueue(self, task_id: str, priority: int, available_at: datetime, idempotency_key: str) -> QueueEnqueueResult:
        current = self._records.get(task_id)
        if current is not None:
            if current.idempotency_key == idempotency_key and current.priority == priority and current.available_at == available_at:
                return QueueEnqueueResult(task_id, created=False)
            raise IdempotencyConflictError("active queue row has conflicting enqueue intent")
        self._records[task_id] = _QueueRecord(task_id, priority, available_at, idempotency_key)
        return QueueEnqueueResult(task_id, created=True)

    def claim(self, worker_id: str, now: datetime, lease_duration: timedelta) -> Lease | None:
        candidates = [record for record in self._records.values() if record.available_at <= now and record.claimed_by is None]
        if not candidates:
            return None
        record = sorted(candidates, key=lambda item: (-item.priority, item.available_at))[0]
        lease = Lease(record.task_id, worker_id, now + lease_duration, record.attempt + 1)
        self._records[record.task_id] = replace(record, claimed_by=worker_id, lease_expires_at=lease.expires_at, attempt=lease.attempt)
        return lease

    def recover_expired(self, now: datetime) -> tuple[QueueFailureResult, ...]:
        results = []
        for task_id, record in list(self._records.items()):
            if record.lease_expires_at is not None and record.lease_expires_at <= now:
                if record.attempt >= 3:
                    del self._records[task_id]
                    results.append(QueueFailureResult(task_id, retry_at=None, terminal=True))
                else:
                    self._records[task_id] = replace(record, claimed_by=None, lease_expires_at=None, available_at=now)
                    results.append(QueueFailureResult(task_id, retry_at=now, terminal=False))
        return tuple(results)

    def heartbeat(self, task_id: str, worker_id: str, now: datetime, lease_duration: timedelta) -> Lease:
        record = self._records.get(task_id)
        if record is None or record.claimed_by != worker_id or record.lease_expires_at is None or record.lease_expires_at <= now:
            raise LeaseLostError("queue lease is not owned or has expired")
        expires_at = now + lease_duration
        self._records[task_id] = replace(record, lease_expires_at=expires_at)
        return Lease(task_id, worker_id, expires_at, record.attempt)

    def complete(self, task_id: str, worker_id: str, now: datetime) -> QueueCompletionResult:
        record = self._records.get(task_id)
        if record is None or record.claimed_by != worker_id or record.lease_expires_at is None or record.lease_expires_at <= now:
            raise LeaseLostError("queue lease is not owned or has expired")
        del self._records[task_id]
        return QueueCompletionResult(task_id, terminal=True)

    def fail(self, task_id: str, worker_id: str, now: datetime, error: str) -> QueueFailureResult:
        record = self._records.get(task_id)
        if record is None or record.claimed_by != worker_id or record.lease_expires_at is None or record.lease_expires_at <= now:
            raise LeaseLostError("queue lease is not owned or has expired")
        if record.attempt >= 3:
            del self._records[task_id]
            return QueueFailureResult(task_id, retry_at=None, terminal=True)
        delay = min(30 * (2 ** (record.attempt - 1)), 900)
        self._records[task_id] = replace(record, claimed_by=None, lease_expires_at=None, available_at=now + timedelta(seconds=delay))
        return QueueFailureResult(task_id, retry_at=now + timedelta(seconds=delay), terminal=False)


class InMemoryFuseStore:
    def __init__(self, state: dict[str, FuseState]) -> None:
        self._state = state

    def get(self) -> FuseState:
        return self._state["value"]

    def set(self, state: FuseState) -> FuseState:
        self._state["value"] = state
        return state


class InMemoryIntakeLedger:
    def __init__(self, records: dict[str, IntakeRecord]) -> None:
        self._records = records
    def get(self, request_id: str) -> IntakeRecord | None:
        return self._records.get(request_id)
    def create(self, record: IntakeRecord) -> IntakeRecord:
        current = self._records.get(record.request_id)
        if current is not None:
            if current == record:
                return current
            raise IdempotencyConflictError("request id has conflicting intake")
        self._records[record.request_id] = record
        return record


class InMemoryControlEventStore:
    def __init__(self, events: list[ControlEvent]) -> None:
        self._events = events
    def append(self, event: ControlEvent) -> ControlEvent:
        self._events.append(event)
        return event
    def read(self) -> tuple[ControlEvent, ...]:
        return tuple(self._events)


class InMemoryUnitOfWork:
    """Copy-on-write UoW used to prove the same commit boundary as PostgreSQL."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._queue_records: dict[str, _QueueRecord] = {}
        self._fuse_state = {"value": FuseState(False, datetime.min)}
        self._intakes: dict[str, IntakeRecord] = {}
        self._control_events: list[ControlEvent] = []
        self._committed = False
        self._bind(self._tasks, self._events, self._event_ids, self._queue_records, self._fuse_state, self._intakes, self._control_events)

    def _bind(self, tasks: dict[str, Task], events: dict[str, list[Event]], event_ids: dict[str, Event], queue_records: dict[str, _QueueRecord], fuse_state: dict[str, FuseState], intakes: dict[str, IntakeRecord], control_events: list[ControlEvent]) -> None:
        self.tasks = InMemoryTaskRepository()
        self.tasks._tasks = tasks
        self.events = InMemoryEventStore()
        self.events._events, self.events._event_ids = events, event_ids
        self.queue = InMemoryTaskQueue(queue_records)
        self.fuse = InMemoryFuseStore(fuse_state)
        self.intake_ledger = InMemoryIntakeLedger(intakes)
        self.control_events = InMemoryControlEventStore(control_events)

    def __enter__(self):
        self._working_tasks = dict(self._tasks)
        self._working_events = {key: list(value) for key, value in self._events.items()}
        self._working_event_ids = dict(self._event_ids)
        self._working_queue_records = dict(self._queue_records)
        self._working_fuse_state = dict(self._fuse_state)
        self._working_intakes = dict(self._intakes)
        self._working_control_events = list(self._control_events)
        self._committed = False
        self._bind(self._working_tasks, self._working_events, self._working_event_ids, self._working_queue_records, self._working_fuse_state, self._working_intakes, self._working_control_events)
        return self

    def commit(self) -> None:
        self._tasks, self._events, self._event_ids, self._queue_records = self._working_tasks, self._working_events, self._working_event_ids, self._working_queue_records
        self._fuse_state, self._intakes, self._control_events = self._working_fuse_state, self._working_intakes, self._working_control_events
        self._committed = True

    def rollback(self) -> None:
        self._committed = False

    def __exit__(self, exc_type, exc, tb) -> None:
        self._bind(self._tasks, self._events, self._event_ids, self._queue_records, self._fuse_state, self._intakes, self._control_events)
