"""PostgreSQL DB-API adapters; one Unit of Work owns each transaction."""

import json
from datetime import datetime, timedelta
from typing import Callable

from meta_loop.application.models import Lease, QueueCompletionResult, QueueEnqueueResult, QueueFailureResult
from meta_loop.domain.enums import EventType, Role
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
