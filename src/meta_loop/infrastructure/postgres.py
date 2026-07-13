"""PostgreSQL DB-API adapters; one Unit of Work owns each transaction."""

import json
from datetime import datetime, timedelta
from typing import Callable

from meta_loop.application.models import CatalogedArtifact, ControlEvent, FuseState, IntakeRecord, Lease, QueueCompletionResult, QueueEnqueueResult, QueueFailureResult, RunnerSessionOutcome, RunnerSessionReceipt, RunnerSessionRecord, RunnerSessionRequest, RunnerSessionState, WorkerIdentity, WorkerResult, WorkerResultReceipt
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import EventType, Role, TaskStatus
from meta_loop.domain.errors import CreateConflictError, IdempotencyConflictError, LeaseLostError, OptimisticConflictError, SequenceConflictError, TaskNotFoundError
from meta_loop.domain.events import Event
from meta_loop.domain.task import Task
from meta_loop.serialization.codecs import event_to_dict, task_from_dict, task_to_dict


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class PostgresTaskRepository:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def get(self, task_id: str) -> Task | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT task_json FROM tasks WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
        return None if row is None else task_from_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0]))

    def create(self, task: Task) -> Task:
        if task.version != 0:
            raise OptimisticConflictError("new task must start at version zero")
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT task_id FROM tasks WHERE task_id = %s FOR UPDATE", (task.task_id,))
            if cursor.fetchone() is not None:
                raise CreateConflictError("task already exists")
            cursor.execute(
                "INSERT INTO tasks (task_id, state, version, effective_risk, created_at, task_json) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (task.task_id, task.status.value, task.version, task.risk.effective.value, task.created_at, _canonical(task_to_dict(task))),
            )
        return task

    def update(self, task: Task, expected_version: int) -> Task:
        if task.version != expected_version + 1:
            raise OptimisticConflictError("new task version must increment by one")
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT version FROM tasks WHERE task_id = %s FOR UPDATE", (task.task_id,))
            row = cursor.fetchone()
            if row is None:
                raise TaskNotFoundError("task does not exist")
            if row[0] != expected_version:
                raise OptimisticConflictError("task version does not match")
            cursor.execute("UPDATE tasks SET state = %s, version = %s, effective_risk = %s, task_json = %s::jsonb, updated_at = now() WHERE task_id = %s", (task.status.value, task.version, task.risk.effective.value, _canonical(task_to_dict(task)), task.task_id))
        return task

    def save(self, task: Task, expected_version: int) -> Task:
        return self.create(task) if task.version == 0 else self.update(task, expected_version)

    def list(self) -> tuple[Task, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT task_json FROM tasks ORDER BY created_at, task_id")
            return tuple(task_from_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0])) for row in cursor.fetchall())


class PostgresEventStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def append(self, event: Event, expected_sequence: int) -> Event:
        canonical = _canonical(event_to_dict(event))
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT task_id FROM tasks WHERE task_id = %s FOR UPDATE", (event.task_id,))
            if cursor.fetchone() is None:
                raise TaskNotFoundError("event task does not exist")
            cursor.execute("SELECT event_id, task_id, event_type, occurred_at, actor, payload, schema_version, causation_id, correlation_id, sequence FROM task_events WHERE event_id = %s", (event.event_id,))
            existing = cursor.fetchone()
            if existing is not None:
                prior = Event(existing[0], existing[1], EventType(existing[2]), existing[3], Role(existing[4]), existing[5], existing[6], existing[7], existing[8], existing[9])
                if _canonical(event_to_dict(prior)) == canonical:
                    return prior
                raise IdempotencyConflictError("event id has different canonical content")
            cursor.execute("SELECT COALESCE(MAX(sequence), 0) FROM task_events WHERE task_id = %s", (event.task_id,))
            current = cursor.fetchone()[0]
            if current != expected_sequence or event.sequence != expected_sequence + 1:
                raise SequenceConflictError("event sequence conflict")
            try:
                cursor.execute("INSERT INTO task_events (event_id, task_id, event_type, occurred_at, actor, payload, schema_version, causation_id, correlation_id, sequence) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)", (event.event_id, event.task_id, event.event_type.value, event.occurred_at, event.actor.value, _canonical(dict(event.payload)), event.schema_version, event.causation_id, event.correlation_id, event.sequence))
            except Exception as error:
                if "task_events_task_id_sequence_key" in str(error):
                    raise SequenceConflictError("event sequence conflict") from error
                raise
        return event

    def read(self, task_id: str) -> tuple[Event, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT event_id, task_id, event_type, occurred_at, actor, payload, schema_version, causation_id, correlation_id, sequence FROM task_events WHERE task_id = %s ORDER BY sequence", (task_id,))
            return tuple(Event(row[0], row[1], EventType(row[2]), row[3], Role(row[4]), row[5], row[6], row[7], row[8], row[9]) for row in cursor.fetchall())


class PostgresTaskQueue:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def enqueue(self, task_id: str, priority: int, available_at: datetime, idempotency_key: str) -> QueueEnqueueResult:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT idempotency_key, priority, available_at FROM task_queue WHERE task_id = %s FOR UPDATE", (task_id,))
            existing = cursor.fetchone()
            if existing is not None:
                if existing[0] == idempotency_key and existing[1] == priority and existing[2] == available_at:
                    return QueueEnqueueResult(task_id, created=False)
                raise IdempotencyConflictError("active queue row has conflicting enqueue intent")
            cursor.execute("INSERT INTO task_queue (task_id, state, priority, available_at, idempotency_key) VALUES (%s, 'ready', %s, %s, %s)", (task_id, priority, available_at, idempotency_key))
        return QueueEnqueueResult(task_id, created=True)

    def claim(self, worker_id: str, now: datetime, lease_duration: timedelta) -> Lease | None:
        with self._connection.cursor() as cursor:
            cursor.execute("""WITH candidate AS (SELECT task_id FROM task_queue WHERE state = 'ready' AND available_at <= %s AND attempt < max_attempts ORDER BY priority DESC, available_at ASC FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE task_queue q SET state = 'leased', claimed_by = %s, claimed_at = %s, lease_expires_at = %s, attempt = q.attempt + 1, updated_at = now() FROM candidate WHERE q.task_id = candidate.task_id RETURNING q.task_id, q.attempt, q.lease_expires_at""", (now, worker_id, now, now + lease_duration))
            row = cursor.fetchone()
        return None if row is None else Lease(row[0], worker_id, row[2], row[1])

    def heartbeat(self, task_id: str, worker_id: str, now: datetime, lease_duration: timedelta) -> Lease:
        with self._connection.cursor() as cursor:
            cursor.execute("UPDATE task_queue SET lease_expires_at = %s, updated_at = now() WHERE task_id = %s AND state = 'leased' AND claimed_by = %s AND lease_expires_at > %s RETURNING attempt, lease_expires_at", (now + lease_duration, task_id, worker_id, now))
            row = cursor.fetchone()
        if row is None:
            raise LeaseLostError("queue lease is not owned or has expired")
        return Lease(task_id, worker_id, row[1], row[0])

    def complete(self, task_id: str, worker_id: str, now: datetime) -> QueueCompletionResult:
        with self._connection.cursor() as cursor:
            cursor.execute("DELETE FROM task_queue WHERE task_id = %s AND state = 'leased' AND claimed_by = %s AND lease_expires_at > %s RETURNING task_id", (task_id, worker_id, now))
            row = cursor.fetchone()
        if row is None:
            raise LeaseLostError("queue lease is not owned or has expired")
        return QueueCompletionResult(task_id, terminal=True)

    def fail(self, task_id: str, worker_id: str, now: datetime, error: str) -> QueueFailureResult:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT attempt, max_attempts FROM task_queue WHERE task_id = %s AND state = 'leased' AND claimed_by = %s AND lease_expires_at > %s FOR UPDATE", (task_id, worker_id, now))
            row = cursor.fetchone()
            if row is None:
                raise LeaseLostError("queue lease is not owned or has expired")
            if row[0] >= row[1]:
                cursor.execute("DELETE FROM task_queue WHERE task_id = %s", (task_id,))
                return QueueFailureResult(task_id, retry_at=None, terminal=True)
            delay = min(30 * (2 ** (row[0] - 1)), 900)
            retry_at = now + timedelta(seconds=delay)
            cursor.execute("UPDATE task_queue SET state = 'ready', claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL, last_error = %s, available_at = %s, updated_at = now() WHERE task_id = %s", (error, retry_at, task_id))
        return QueueFailureResult(task_id, retry_at=retry_at, terminal=False)

    def recover_expired(self, now: datetime) -> tuple[QueueFailureResult, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("DELETE FROM task_queue WHERE state = 'leased' AND lease_expires_at <= %s AND attempt >= max_attempts RETURNING task_id", (now,))
            results = [QueueFailureResult(row[0], retry_at=None, terminal=True) for row in cursor.fetchall()]
            cursor.execute("UPDATE task_queue SET state = 'ready', claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL, available_at = %s, updated_at = now() WHERE state = 'leased' AND lease_expires_at <= %s AND attempt < max_attempts RETURNING task_id", (now, now))
            results.extend(QueueFailureResult(row[0], retry_at=now, terminal=False) for row in cursor.fetchall())
        return tuple(results)


class PostgresFuseStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def get(self) -> FuseState:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT engaged, changed_at, governance_revision FROM system_fuse WHERE singleton = TRUE")
            row = cursor.fetchone()
        return FuseState(row[0], row[1], row[2])

    def set(self, state: FuseState) -> FuseState:
        with self._connection.cursor() as cursor:
            cursor.execute("UPDATE system_fuse SET engaged = %s, changed_at = %s, governance_revision = %s WHERE singleton = TRUE", (state.engaged, state.changed_at, state.governance_revision))
        return state

class PostgresIntakeLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def get(self, request_id: str) -> IntakeRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, task_id FROM intake_ledger WHERE request_id = %s", (request_id,))
            row = cursor.fetchone()
        return None if row is None else IntakeRecord(request_id, row[0] if isinstance(row[0], str) else _canonical(row[0]), row[1])

    def create(self, record: IntakeRecord) -> IntakeRecord:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, task_id FROM intake_ledger WHERE request_id = %s FOR UPDATE", (record.request_id,))
            row = cursor.fetchone()
            if row is not None:
                existing = IntakeRecord(record.request_id, row[0] if isinstance(row[0], str) else _canonical(row[0]), row[1])
                if existing == record:
                    return existing
                raise IdempotencyConflictError("request id has conflicting intake")
            cursor.execute("INSERT INTO intake_ledger (request_id, request_json, task_id) VALUES (%s, %s::jsonb, %s)", (record.request_id, record.canonical_request, record.task_id))
        return record


class PostgresControlEventStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def append(self, event: ControlEvent) -> ControlEvent:
        with self._connection.cursor() as cursor:
            cursor.execute("INSERT INTO control_events (action, occurred_at, governance_revision, schema_version) VALUES (%s, %s, %s, 1)", (event.action, event.occurred_at, event.governance_revision))
        return event

    def read(self) -> tuple[ControlEvent, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT action, occurred_at, governance_revision FROM control_events ORDER BY sequence")
            return tuple(ControlEvent(row[0], row[1], row[2]) for row in cursor.fetchall())


class PostgresArtifactCatalog:
    def __init__(self, connection: object) -> None:
        self._connection = connection
    def register(self, artifact: CatalogedArtifact) -> CatalogedArtifact:
        ref = artifact.reference
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT logical_name, media_type, classification, source_kind, size_bytes FROM artifact_catalog WHERE digest = %s FOR UPDATE", (ref.digest.value,))
            row = cursor.fetchone()
            if row is not None:
                current = CatalogedArtifact(ArtifactRef(ArtifactDigest(ref.digest.value), row[0], row[1]), row[2], row[3], row[4])
                if current != artifact:
                    raise IdempotencyConflictError("artifact digest has conflicting metadata")
                return current
            cursor.execute("INSERT INTO artifact_catalog (digest, logical_name, media_type, classification, source_kind, size_bytes) VALUES (%s, %s, %s, %s, %s, %s)", (ref.digest.value, ref.logical_name, ref.media_type, artifact.classification, artifact.source_kind, artifact.size_bytes))
        return artifact
    def get(self, digest: str) -> CatalogedArtifact | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT logical_name, media_type, classification, source_kind, size_bytes FROM artifact_catalog WHERE digest = %s", (digest,))
            row = cursor.fetchone()
        return None if row is None else CatalogedArtifact(ArtifactRef(ArtifactDigest(digest), row[0], row[1]), row[2], row[3], row[4])
    def digests(self) -> tuple[str, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT digest FROM artifact_catalog ORDER BY digest")
            return tuple(row[0] for row in cursor.fetchall())


def _runner_request_from_dict(data: dict[str, object]) -> RunnerSessionRequest:
    artifacts = tuple(
        ArtifactRef(ArtifactDigest(str(item["digest"]).removeprefix("sha256:")), str(item["logical_name"]), str(item["media_type"]), int(item["schema_version"]))
        for item in data["input_artifacts"]
    )
    return RunnerSessionRequest(str(data["session_id"]), str(data["task_id"]), Role(str(data["role"])), int(data["expected_task_version"]), int(data["expected_sequence"]), artifacts, int(data["timeout_seconds"]), str(data["correlation_id"]), int(data["schema_version"]))


class PostgresRunnerSessionStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def record_once(self, request: RunnerSessionRequest) -> RunnerSessionReceipt:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, state FROM runner_sessions WHERE session_id = %s FOR UPDATE", (request.session_id,))
            row = cursor.fetchone()
            if row is not None:
                prior = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if _canonical(prior) != request.canonical():
                    raise IdempotencyConflictError("runner session id has conflicting request")
                return RunnerSessionReceipt(request.session_id, RunnerSessionState(row[1]), False)
            cursor.execute("INSERT INTO runner_sessions (session_id, task_id, role, expected_task_version, expected_sequence, request_json, state, schema_version) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'requested', 1)", (request.session_id, request.task_id, request.role.value, request.expected_task_version, request.expected_sequence, request.canonical()))
        return RunnerSessionReceipt(request.session_id, RunnerSessionState.REQUESTED, True)

    def get(self, session_id: str) -> RunnerSessionRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, state, outcome_json FROM runner_sessions WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        outcome = None if row[2] is None else RunnerSessionOutcome(session_id, RunnerSessionState(row[2]["state"]), row[2]["summary"])
        return RunnerSessionRecord(_runner_request_from_dict(data), RunnerSessionState(row[1]), outcome)

    def set_outcome(self, outcome: RunnerSessionOutcome) -> RunnerSessionRecord:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT outcome_json FROM runner_sessions WHERE session_id = %s FOR UPDATE", (outcome.session_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("runner session does not exist")
            canonical = _canonical({"state": outcome.state.value, "summary": outcome.summary})
            if row[0] is not None:
                prior = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if _canonical(prior) != canonical:
                    raise IdempotencyConflictError("runner session has conflicting outcome")
            else:
                cursor.execute("UPDATE runner_sessions SET state = %s, outcome_json = %s::jsonb, updated_at = now() WHERE session_id = %s", (outcome.state.value, canonical, outcome.session_id))
        record = self.get(outcome.session_id)
        return record


class PostgresWorkerResultLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def record_once(self, result: WorkerResult) -> WorkerResultReceipt:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT result_json FROM worker_results WHERE result_id = %s FOR UPDATE", (result.result_id,))
            row = cursor.fetchone()
            if row is not None:
                prior = row[0] if isinstance(row[0], str) else _canonical(row[0])
                if prior != result.canonical():
                    raise IdempotencyConflictError("worker result id has conflicting content")
                return WorkerResultReceipt(result.result_id, False)
            cursor.execute("INSERT INTO worker_results (result_id, session_id, task_id, result_json, schema_version) VALUES (%s, %s, %s, %s::jsonb, 1)", (result.result_id, result.session_id, result.task_id, result.canonical()))
        return WorkerResultReceipt(result.result_id, True)

    def get(self, result_id: str) -> WorkerResult | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT result_json FROM worker_results WHERE result_id = %s", (result_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return WorkerResult(str(value["result_id"]), str(value["session_id"]), str(value["task_id"]), WorkerIdentity(str(value["worker_id"]), Role(str(value["role"]))), TaskStatus(str(value["target"])), str(value["summary"]), int(value["expected_task_version"]), int(value["expected_sequence"]), int(value["schema_version"]))


class PostgresUnitOfWork:
    def __init__(self, connection_factory: Callable[[], object]) -> None:
        self._connection_factory = connection_factory
        self._connection = None
        self._committed = False

    def __enter__(self):
        self._connection = self._connection_factory()
        self.tasks = PostgresTaskRepository(self._connection)
        self.events = PostgresEventStore(self._connection)
        self.queue = PostgresTaskQueue(self._connection)
        self.fuse = PostgresFuseStore(self._connection)
        self.intake_ledger = PostgresIntakeLedger(self._connection)
        self.control_events = PostgresControlEventStore(self._connection)
        self.artifacts = PostgresArtifactCatalog(self._connection)
        self.runner_sessions = PostgresRunnerSessionStore(self._connection)
        self.worker_results = PostgresWorkerResultLedger(self._connection)
        self._committed = False
        return self

    def commit(self) -> None:
        self._connection.commit()
        self._committed = True

    def rollback(self) -> None:
        self._connection.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None or not self._committed:
            self._connection.rollback()
        self._connection.close()
