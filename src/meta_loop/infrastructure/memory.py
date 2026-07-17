from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import uuid4

from meta_loop.application.models import CatalogedArtifact, ControlEvent, FuseState, IntakeRecord, IssueIngestionRecord, Lease, QueueCompletionResult, QueueEnqueueResult, QueueFailureResult, RunnerSessionOutcome, RunnerSessionReceipt, RunnerSessionRecord, RunnerSessionRequest, RunnerSessionState, WorkerResult, WorkerResultReceipt, WorkspaceRecord, WorkspaceState
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


class UUIDIdGenerator:
    """Runtime-safe event identifiers; deterministic IDs remain test-only."""

    def new(self) -> str:
        return str(uuid4())


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
    def locked_get(self) -> FuseState:
        return self.get()

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


class InMemorySourceIngestionLedger:
    def __init__(self, records: dict[str, IssueIngestionRecord]) -> None:
        self._records = records
    def reserve(self, source_key: str, trigger_event_id: str) -> None:
        return None
    def get(self, source_key: str) -> IssueIngestionRecord | None:
        return self._records.get(source_key)
    def get_by_trigger_event(self, trigger_event_id: str) -> IssueIngestionRecord | None:
        return next((record for record in self._records.values() if record.trigger_event_id == trigger_event_id), None)
    def record_once(self, record: IssueIngestionRecord) -> tuple[IssueIngestionRecord, bool]:
        current = self._records.get(record.source_key)
        if current is not None:
            if current != record:
                raise IdempotencyConflictError("source has conflicting canonical content")
            return current, False
        if self.get_by_trigger_event(record.trigger_event_id) is not None:
            raise IdempotencyConflictError("trigger event is already associated with another source")
        self._records[record.source_key] = record
        return record, True


class InMemoryControlEventStore:
    def __init__(self, events: list[ControlEvent]) -> None:
        self._events = events
    def append(self, event: ControlEvent) -> ControlEvent:
        self._events.append(event)
        return event
    def read(self) -> tuple[ControlEvent, ...]:
        return tuple(self._events)


class InMemoryArtifactCatalog:
    def __init__(self, artifacts: dict[str, CatalogedArtifact]) -> None:
        self._artifacts = artifacts
    def register(self, artifact: CatalogedArtifact) -> CatalogedArtifact:
        digest = artifact.reference.digest.value
        current = self._artifacts.get(digest)
        if current is not None and current != artifact:
            raise IdempotencyConflictError("artifact digest has conflicting metadata")
        self._artifacts[digest] = artifact
        return artifact
    def get(self, digest: str) -> CatalogedArtifact | None:
        return self._artifacts.get(digest)
    def digests(self) -> tuple[str, ...]:
        return tuple(self._artifacts)


class InMemoryRunnerSessionStore:
    def __init__(self, records: dict[str, RunnerSessionRecord]) -> None:
        self._records = records

    def record_once(self, request: RunnerSessionRequest) -> RunnerSessionReceipt:
        current = self._records.get(request.session_id)
        if current is not None:
            if current.request.canonical() != request.canonical():
                raise IdempotencyConflictError("runner session id has conflicting request")
            return RunnerSessionReceipt(request.session_id, current.state, False)
        self._records[request.session_id] = RunnerSessionRecord(request)
        return RunnerSessionReceipt(request.session_id, RunnerSessionState.REQUESTED, True)

    def get(self, session_id: str) -> RunnerSessionRecord | None:
        return self._records.get(session_id)

    def set_outcome(self, outcome: RunnerSessionOutcome) -> RunnerSessionRecord:
        current = self._records.get(outcome.session_id)
        if current is None:
            raise ValidationError("runner session does not exist")
        if current.outcome is not None:
            if current.outcome == outcome:
                return current
            raise IdempotencyConflictError("runner session has conflicting outcome")
        updated = replace(current, state=outcome.state, outcome=outcome)
        self._records[outcome.session_id] = updated
        return updated


class InMemoryWorkerResultLedger:
    def __init__(self, results: dict[str, WorkerResult]) -> None:
        self._results = results

    def record_once(self, result: WorkerResult) -> WorkerResultReceipt:
        current = self._results.get(result.result_id)
        if current is not None:
            if current.canonical() != result.canonical():
                raise IdempotencyConflictError("worker result id has conflicting content")
            return WorkerResultReceipt(result.result_id, False)
        self._results[result.result_id] = result
        return WorkerResultReceipt(result.result_id, True)

    def get(self, result_id: str) -> WorkerResult | None:
        return self._results.get(result_id)


class InMemoryWorkspaceLedger:
    def __init__(self, records: dict[str, WorkspaceRecord]) -> None:
        self._records = records

    def record_once(self, record: WorkspaceRecord):
        current = self._records.get(record.workspace_id)
        if current is not None:
            if current.request.canonical() != record.request.canonical():
                raise IdempotencyConflictError("workspace allocation id has conflicting content")
            return current, False
        if any(value.state is WorkspaceState.ACTIVE and value.request.task_id == record.request.task_id and value.request.purpose == record.request.purpose for value in self._records.values()):
            raise IdempotencyConflictError("task already has an active workspace for this purpose")
        self._records[record.workspace_id] = record
        return record, True

    def get(self, allocation_id: str) -> WorkspaceRecord | None:
        return self._records.get(allocation_id)

    def release(self, allocation_id: str) -> WorkspaceRecord:
        current = self._records.get(allocation_id)
        if current is None:
            raise ValidationError("workspace allocation does not exist")
        if current.state is WorkspaceState.RELEASED:
            return current
        released = replace(current, state=WorkspaceState.RELEASED)
        self._records[allocation_id] = released
        return released


class InMemoryUnitOfWork:
    """Copy-on-write UoW used to prove the same commit boundary as PostgreSQL."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._queue_records: dict[str, _QueueRecord] = {}
        self._fuse_state = {"value": FuseState(False, datetime.min)}
        self._intakes: dict[str, IntakeRecord] = {}
        self._source_ingestions: dict[str, IssueIngestionRecord] = {}
        self._control_events: list[ControlEvent] = []
        self._artifacts: dict[str, CatalogedArtifact] = {}
        self._runner_sessions: dict[str, RunnerSessionRecord] = {}
        self._worker_results: dict[str, WorkerResult] = {}
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._committed = False
        self._bind(self._tasks, self._events, self._event_ids, self._queue_records, self._fuse_state, self._intakes, self._source_ingestions, self._control_events, self._artifacts, self._runner_sessions, self._worker_results, self._workspaces)

    def _bind(self, tasks: dict[str, Task], events: dict[str, list[Event]], event_ids: dict[str, Event], queue_records: dict[str, _QueueRecord], fuse_state: dict[str, FuseState], intakes: dict[str, IntakeRecord], source_ingestions: dict[str, IssueIngestionRecord], control_events: list[ControlEvent], artifacts: dict[str, CatalogedArtifact], runner_sessions: dict[str, RunnerSessionRecord], worker_results: dict[str, WorkerResult], workspaces: dict[str, WorkspaceRecord]) -> None:
        self.tasks = InMemoryTaskRepository()
        self.tasks._tasks = tasks
        self.events = InMemoryEventStore()
        self.events._events, self.events._event_ids = events, event_ids
        self.queue = InMemoryTaskQueue(queue_records)
        self.fuse = InMemoryFuseStore(fuse_state)
        self.intake_ledger = InMemoryIntakeLedger(intakes)
        self.source_ingestions = InMemorySourceIngestionLedger(source_ingestions)
        self.control_events = InMemoryControlEventStore(control_events)
        self.artifacts = InMemoryArtifactCatalog(artifacts)
        self.runner_sessions = InMemoryRunnerSessionStore(runner_sessions)
        self.worker_results = InMemoryWorkerResultLedger(worker_results)
        self.workspaces = InMemoryWorkspaceLedger(workspaces)

    def __enter__(self):
        self._working_tasks = dict(self._tasks)
        self._working_events = {key: list(value) for key, value in self._events.items()}
        self._working_event_ids = dict(self._event_ids)
        self._working_queue_records = dict(self._queue_records)
        self._working_fuse_state = dict(self._fuse_state)
        self._working_intakes = dict(self._intakes)
        self._working_source_ingestions = dict(self._source_ingestions)
        self._working_control_events = list(self._control_events)
        self._working_artifacts = dict(self._artifacts)
        self._working_runner_sessions = dict(self._runner_sessions)
        self._working_worker_results = dict(self._worker_results)
        self._working_workspaces = dict(self._workspaces)
        self._committed = False
        self._bind(self._working_tasks, self._working_events, self._working_event_ids, self._working_queue_records, self._working_fuse_state, self._working_intakes, self._working_source_ingestions, self._working_control_events, self._working_artifacts, self._working_runner_sessions, self._working_worker_results, self._working_workspaces)
        return self

    def commit(self) -> None:
        self._tasks, self._events, self._event_ids, self._queue_records = self._working_tasks, self._working_events, self._working_event_ids, self._working_queue_records
        self._fuse_state, self._intakes, self._source_ingestions, self._control_events = self._working_fuse_state, self._working_intakes, self._working_source_ingestions, self._working_control_events
        self._artifacts = self._working_artifacts
        self._runner_sessions = self._working_runner_sessions
        self._worker_results = self._working_worker_results
        self._workspaces = self._working_workspaces
        self._committed = True

    def rollback(self) -> None:
        self._committed = False

    def __exit__(self, exc_type, exc, tb) -> None:
        self._bind(self._tasks, self._events, self._event_ids, self._queue_records, self._fuse_state, self._intakes, self._source_ingestions, self._control_events, self._artifacts, self._runner_sessions, self._worker_results, self._workspaces)
