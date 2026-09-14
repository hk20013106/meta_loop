"""PostgreSQL DB-API adapters; one Unit of Work owns each transaction."""

import json
from datetime import datetime, timedelta
from typing import Callable

try:
    from psycopg.errors import UniqueViolation
except ImportError:  # psycopg is optional outside PostgreSQL integration tests.
    UniqueViolation = ()

from meta_loop.application.models import CatalogedArtifact, CheckRunConclusion, CheckRunObservation, CheckRunStatus, ControlEvent, FuseState, IntakeRecord, IssueIngestionRecord, Lease, PublicationEffectIntent, PublicationEffectRecord, PublicationEffectState, PublicationIntent, PublicationReceipt, PublicationRecord, PublicationReviewDecision, PreparedHead, PublicationState, PullRequestReceipt, QueueCompletionResult, QueueEnqueueResult, QueueFailureResult, ReviewDecision, RunnerSessionOutcome, RunnerSessionReceipt, RunnerSessionRecord, RunnerSessionRequest, RunnerSessionState, VerificationResult, VerificationResultCode, VerificationStatus, VerifierProfile, WorkerIdentity, WorkerResult, WorkerResultReceipt, WorkspaceRecord, WorkspaceRequest, WorkspaceState
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import EventType, Role, TaskStatus
from meta_loop.domain.errors import CreateConflictError, IdempotencyConflictError, LeaseLostError, OptimisticConflictError, SequenceConflictError, TaskNotFoundError, ValidationError
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

    def locked_get(self) -> FuseState:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT engaged, changed_at, governance_revision FROM system_fuse WHERE singleton = TRUE FOR UPDATE")
            row = cursor.fetchone()
        if row is None:
            raise ValidationError("system fuse bootstrap row is missing")
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


class PostgresSourceIngestionLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def reserve(self, source_key: str, trigger_event_id: str) -> None:
        with self._connection.cursor() as cursor:
            for value in sorted(("source:" + source_key, "trigger:" + trigger_event_id)):
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (value,))

    def get(self, source_key: str) -> IssueIngestionRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_request, task_id, trigger_event_id FROM source_ingestions WHERE source_key = %s", (source_key,))
            row = cursor.fetchone()
        return None if row is None else IssueIngestionRecord(source_key, row[0] if isinstance(row[0], str) else _canonical(row[0]), row[1], row[2])

    def get_by_trigger_event(self, trigger_event_id: str) -> IssueIngestionRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT source_key, canonical_request, task_id, trigger_event_id FROM source_ingestions WHERE trigger_event_id = %s", (trigger_event_id,))
            row = cursor.fetchone()
        return None if row is None else IssueIngestionRecord(row[0], row[1] if isinstance(row[1], str) else _canonical(row[1]), row[2], row[3])

    def record_once(self, record: IssueIngestionRecord) -> tuple[IssueIngestionRecord, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_request, task_id, trigger_event_id FROM source_ingestions WHERE source_key = %s FOR UPDATE", (record.source_key,))
            row = cursor.fetchone()
            if row is not None:
                existing = IssueIngestionRecord(record.source_key, row[0] if isinstance(row[0], str) else _canonical(row[0]), row[1], row[2])
                if existing != record:
                    raise IdempotencyConflictError("source has conflicting canonical content")
                return existing, False
            try:
                cursor.execute("INSERT INTO source_ingestions (source_key, task_id, trigger_event_id, canonical_request, schema_version) VALUES (%s, %s, %s, %s::jsonb, 1)", (record.source_key, record.task_id, record.trigger_event_id, record.canonical_request))
            except UniqueViolation as error:
                raise IdempotencyConflictError("source ingestion conflicts with an existing trigger") from error
        return record, True


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


def _workspace_request_from_dict(data: dict[str, object]) -> WorkspaceRequest:
    from meta_loop.application.models import WorkspacePurpose
    return WorkspaceRequest(str(data["allocation_id"]), str(data["task_id"]), Role(str(data["role"])), WorkspacePurpose(str(data["purpose"])), str(data["source_revision"]), int(data["expected_task_version"]), int(data["expected_sequence"]), str(data["correlation_id"]), data.get("governance_revision"), int(data["schema_version"]))


class PostgresWorkspaceLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def record_once(self, record: WorkspaceRecord):
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, read_only, repository_name, state FROM workspace_allocations WHERE allocation_id = %s FOR UPDATE", (record.workspace_id,))
            row = cursor.fetchone()
            if row is not None:
                prior = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if _canonical(prior) != record.request.canonical() or row[1] != record.read_only or row[2] != record.repository_name:
                    raise IdempotencyConflictError("workspace allocation id has conflicting content")
                return WorkspaceRecord(_workspace_request_from_dict(prior), row[1], row[2], WorkspaceState(row[3])), False
            cursor.execute("SELECT allocation_id FROM workspace_allocations WHERE task_id = %s AND purpose = %s AND state = 'active' FOR UPDATE", (record.request.task_id, record.request.purpose.value))
            if cursor.fetchone() is not None:
                raise IdempotencyConflictError("task already has an active workspace for this purpose")
            cursor.execute("INSERT INTO workspace_allocations (allocation_id, task_id, repository_name, role, purpose, source_revision, request_json, read_only, state, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'active', 1)", (record.workspace_id, record.request.task_id, record.repository_name, record.request.role.value, record.request.purpose.value, record.request.source_revision, record.request.canonical(), record.read_only))
        return record, True

    def get(self, allocation_id: str) -> WorkspaceRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, read_only, repository_name, state FROM workspace_allocations WHERE allocation_id = %s", (allocation_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return WorkspaceRecord(_workspace_request_from_dict(value), row[1], row[2], WorkspaceState(row[3]))

    def release(self, allocation_id: str) -> WorkspaceRecord:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT request_json, read_only, repository_name, state FROM workspace_allocations WHERE allocation_id = %s FOR UPDATE", (allocation_id,))
            row = cursor.fetchone()
            if row is None:
                raise TaskNotFoundError("workspace allocation does not exist")
            if row[3] == WorkspaceState.ACTIVE.value:
                cursor.execute("UPDATE workspace_allocations SET state = 'released', updated_at = now() WHERE allocation_id = %s", (allocation_id,))
            value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return WorkspaceRecord(_workspace_request_from_dict(value), row[1], row[2], WorkspaceState.RELEASED)


def _phase8_json(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else json.loads(value)


def _publication_intent_from_dict(data: dict[str, object]) -> PublicationIntent:
    return PublicationIntent(str(data["publication_id"]), str(data["task_id"]), str(data["worker_result_id"]), str(data["patch_digest"]), str(data["repository_name"]), str(data["base_sha"]), int(data["expected_task_version"]), int(data["expected_sequence"]), str(data["correlation_id"]), str(data["governance_revision"]), int(data["schema_version"]))


def _prepared_head_from_row(publication_id: str, patch_digest: str, base_sha: str, tree_sha: str | None, head_sha: str | None, deterministic_ref: str | None) -> PreparedHead | None:
    if tree_sha is None or head_sha is None or deterministic_ref is None:
        return None
    return PreparedHead(publication_id, patch_digest, base_sha, tree_sha, head_sha, deterministic_ref)


def _verification_from_dict(data: dict[str, object] | None) -> VerificationResult | None:
    if data is None:
        return None
    return VerificationResult(str(data["publication_id"]), str(data["head_sha"]), str(data["verifier_image_digest"]), VerifierProfile(str(data["verifier_profile"])), VerificationStatus(str(data["status"])), VerificationResultCode(str(data["result_code"])), int(data["schema_version"]))


class PostgresPublicationLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def _record(self, row) -> PublicationRecord:
        intent = _publication_intent_from_dict(_phase8_json(row[0]))
        prepared = _prepared_head_from_row(intent.publication_id, intent.patch_digest, intent.base_sha, row[1], row[2], row[3])
        verification = _verification_from_dict(None if row[4] is None else _phase8_json(row[4]))
        return PublicationRecord(intent, PublicationState(row[5]), row[6], prepared, verification)

    def _references_are_valid(self, cursor, intent: PublicationIntent) -> bool:
        cursor.execute("SELECT task_json FROM tasks WHERE task_id = %s FOR UPDATE", (intent.task_id,))
        task_row = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM task_events WHERE task_id = %s", (intent.task_id,))
        event_count = cursor.fetchone()[0]
        cursor.execute("SELECT task_id, session_id, result_json FROM worker_results WHERE result_id = %s", (intent.worker_result_id,))
        result_row = cursor.fetchone()
        cursor.execute("SELECT media_type, source_kind FROM artifact_catalog WHERE digest = %s", (intent.patch_digest,))
        artifact_row = cursor.fetchone()
        if task_row is None or result_row is None or artifact_row is None or result_row[0] != intent.task_id or artifact_row[0] != "text/x-diff" or artifact_row[1] != f"worker_result:{intent.worker_result_id}":
            return False
        task = task_from_dict(_phase8_json(task_row[0]))
        cursor.execute("SELECT task_id, role, state FROM runner_sessions WHERE session_id = %s", (result_row[1],))
        session = cursor.fetchone()
        worker = _phase8_json(result_row[2])
        patch_digests = {artifact.digest.value for artifact in task.artifacts}
        return task.repository.name == intent.repository_name and task.status is TaskStatus.READY_TO_PUBLISH and task.version == intent.expected_task_version and event_count == intent.expected_sequence and intent.patch_digest in patch_digests and session is not None and session[0] == intent.task_id and session[1] == Role.IMPLEMENTER.value and session[2] == RunnerSessionState.SUCCEEDED.value and worker.get("role") == Role.IMPLEMENTER.value

    def reserve(self, intent: PublicationIntent) -> tuple[PublicationRecord, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_input, tree_sha, head_sha, deterministic_ref, verification_json, state, version FROM publications WHERE publication_id = %s FOR UPDATE", (intent.publication_id,))
            row = cursor.fetchone()
            if row is not None:
                record = self._record(row)
                if record.intent.canonical() != intent.canonical():
                    raise IdempotencyConflictError("publication id has conflicting canonical input")
                return record, False
            if not self._references_are_valid(cursor, intent):
                raise ValidationError("publication references are invalid")
            cursor.execute("INSERT INTO publications (publication_id, task_id, worker_result_id, patch_digest, repository_name, base_sha, canonical_input, state, version, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'reserved', 0, 1) ON CONFLICT (publication_id) DO NOTHING", (intent.publication_id, intent.task_id, intent.worker_result_id, intent.patch_digest, intent.repository_name, intent.base_sha, intent.canonical()))
            if cursor.rowcount == 0:
                cursor.execute("SELECT canonical_input, tree_sha, head_sha, deterministic_ref, verification_json, state, version FROM publications WHERE publication_id = %s FOR UPDATE", (intent.publication_id,))
                concurrent = cursor.fetchone()
                if concurrent is None:
                    raise IdempotencyConflictError("publication id conflicts with a concurrent reservation")
                record = self._record(concurrent)
                if record.intent.canonical() != intent.canonical():
                    raise IdempotencyConflictError("publication id has conflicting canonical input")
                return record, False
        return PublicationRecord(intent), True

    def get(self, publication_id: str) -> PublicationRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_input, tree_sha, head_sha, deterministic_ref, verification_json, state, version FROM publications WHERE publication_id = %s", (publication_id,))
            row = cursor.fetchone()
        return None if row is None else self._record(row)

    def record_prepared(self, prepared: PreparedHead, expected_version: int) -> PublicationRecord:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_input, tree_sha, head_sha, deterministic_ref, verification_json, state, version FROM publications WHERE publication_id = %s FOR UPDATE", (prepared.publication_id,))
            row = cursor.fetchone()
            if row is None:
                raise OptimisticConflictError("publication version does not match")
            current = self._record(row)
            if current.version != expected_version or current.state is not PublicationState.RESERVED:
                raise OptimisticConflictError("publication version does not match")
            if current.intent.patch_digest != prepared.patch_digest or current.intent.base_sha != prepared.base_sha:
                raise IdempotencyConflictError("prepared head conflicts with immutable publication input")
            cursor.execute("UPDATE publications SET tree_sha = %s, head_sha = %s, deterministic_ref = %s, state = 'prepared', version = %s, updated_at = now() WHERE publication_id = %s", (prepared.tree_sha, prepared.head_sha, prepared.deterministic_ref, current.version + 1, prepared.publication_id))
        return PublicationRecord(current.intent, PublicationState.PREPARED, current.version + 1, prepared)

    def record_verification(self, verification: VerificationResult, expected_version: int) -> PublicationRecord:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT canonical_input, tree_sha, head_sha, deterministic_ref, verification_json, state, version FROM publications WHERE publication_id = %s FOR UPDATE", (verification.publication_id,))
            row = cursor.fetchone()
            if row is None:
                raise OptimisticConflictError("publication version does not match")
            current = self._record(row)
            if current.version != expected_version or current.state is not PublicationState.PREPARED or current.prepared_head is None:
                raise OptimisticConflictError("publication version does not match")
            if current.prepared_head.head_sha != verification.head_sha:
                raise IdempotencyConflictError("verification head conflicts with prepared head")
            cursor.execute("UPDATE publications SET verification_json = %s::jsonb, state = 'verified', version = %s, updated_at = now() WHERE publication_id = %s", (verification.canonical(), current.version + 1, verification.publication_id))
        return PublicationRecord(current.intent, PublicationState.VERIFIED, current.version + 1, current.prepared_head, verification)


def _decision_from_row(row) -> PublicationReviewDecision:
    data = _phase8_json(row[0])
    artifact = ArtifactRef(ArtifactDigest(str(data["approval_artifact_digest"])), row[1], row[2])
    return PublicationReviewDecision(str(data["approval_id"]), str(data["publication_id"]), str(data["task_id"]), str(data["session_id"]), str(data["result_id"]), artifact, str(data["patch_digest"]), str(data["base_sha"]), str(data["tree_sha"]), str(data["head_sha"]), str(data["reviewer_id"]), ReviewDecision(str(data["decision"])), datetime.fromisoformat(str(data["decided_at"])), int(data["schema_version"]))


class PostgresPublicationApprovalStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def append(self, decision: PublicationReviewDecision) -> tuple[PublicationReviewDecision, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT decision_json, logical_name, media_type FROM publication_approvals JOIN artifact_catalog ON artifact_catalog.digest = publication_approvals.approval_artifact_digest WHERE approval_id = %s FOR UPDATE", (decision.approval_id,))
            row = cursor.fetchone()
            if row is not None:
                prior = _decision_from_row(row)
                if prior.canonical() != decision.canonical():
                    raise IdempotencyConflictError("approval id has conflicting content")
                return prior, False
            cursor.execute("SELECT task_id, patch_digest, base_sha, tree_sha, head_sha, state, verification_json FROM publications WHERE publication_id = %s FOR UPDATE", (decision.publication_id,))
            publication = cursor.fetchone()
            cursor.execute("SELECT digest FROM artifact_catalog WHERE digest = %s", (decision.approval_artifact.digest.value,))
            artifact = cursor.fetchone()
            cursor.execute("SELECT task_id, role, state FROM runner_sessions WHERE session_id = %s", (decision.session_id,))
            session = cursor.fetchone()
            cursor.execute("SELECT task_id, session_id, result_json FROM worker_results WHERE result_id = %s", (decision.result_id,))
            result = cursor.fetchone()
            verification = None if publication is None or publication[6] is None else _phase8_json(publication[6])
            worker = None if result is None else _phase8_json(result[2])
            if publication is None or artifact is None or session is None or result is None or verification is None or publication[5] != PublicationState.VERIFIED.value or verification.get("status") != VerificationStatus.SUCCEEDED.value or publication[0] != decision.task_id or publication[1] != decision.patch_digest or publication[2] != decision.base_sha or publication[3] != decision.tree_sha or publication[4] != decision.head_sha or session[0] != decision.task_id or session[1] != Role.PATCH_REVIEWER.value or session[2] != RunnerSessionState.SUCCEEDED.value or result[0] != decision.task_id or result[1] != decision.session_id or worker.get("role") != Role.PATCH_REVIEWER.value or worker.get("worker_id") != decision.reviewer_id:
                raise IdempotencyConflictError("approval does not match exact publication head")
            cursor.execute("INSERT INTO publication_approvals (approval_id, publication_id, task_id, session_id, result_id, approval_artifact_digest, patch_digest, base_sha, tree_sha, head_sha, reviewer_id, decision, decided_at, decision_json, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 1) ON CONFLICT (approval_id) DO NOTHING", (decision.approval_id, decision.publication_id, decision.task_id, decision.session_id, decision.result_id, decision.approval_artifact.digest.value, decision.patch_digest, decision.base_sha, decision.tree_sha, decision.head_sha, decision.reviewer_id, decision.decision.value, decision.decided_at, decision.canonical()))
            if cursor.rowcount == 0:
                cursor.execute("SELECT decision_json, logical_name, media_type FROM publication_approvals JOIN artifact_catalog ON artifact_catalog.digest = publication_approvals.approval_artifact_digest WHERE approval_id = %s FOR UPDATE", (decision.approval_id,))
                concurrent = cursor.fetchone()
                if concurrent is None:
                    raise IdempotencyConflictError("approval id conflicts with a concurrent append")
                prior = _decision_from_row(concurrent)
                if prior.canonical() != decision.canonical():
                    raise IdempotencyConflictError("approval id has conflicting content")
                return prior, False
        return decision, True

    def read(self, publication_id: str, head_sha: str) -> tuple[PublicationReviewDecision, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT decision_json, logical_name, media_type FROM publication_approvals JOIN artifact_catalog ON artifact_catalog.digest = publication_approvals.approval_artifact_digest WHERE publication_id = %s AND head_sha = %s ORDER BY publication_approvals.created_at, publication_approvals.approval_id", (publication_id, head_sha))
            return tuple(_decision_from_row(row) for row in cursor.fetchall())


def _effect_intent_from_dict(data: dict[str, object]) -> PublicationEffectIntent:
    return PublicationEffectIntent(str(data["effect_id"]), str(data["publication_id"]), str(data["head_sha"]), str(data["deterministic_ref"]), int(data["schema_version"]))


def _receipt_from_dict(data: dict[str, object] | None) -> PublicationReceipt | None:
    if data is None:
        return None
    pull_request_data = data.get("pull_request")
    pull_request = None if pull_request_data is None else PullRequestReceipt(str(pull_request_data["publication_id"]), int(pull_request_data["pull_request_number"]), str(pull_request_data["base_sha"]), str(pull_request_data["tree_sha"]), str(pull_request_data["head_sha"]), int(pull_request_data["schema_version"]))
    return PublicationReceipt(str(data["publication_id"]), str(data["effect_id"]), PublicationEffectState(str(data["state"])), pull_request, int(data["schema_version"]))


class PostgresPublicationEffectStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def _record(self, row) -> PublicationEffectRecord:
        return PublicationEffectRecord(_effect_intent_from_dict(_phase8_json(row[0])), _receipt_from_dict(None if row[1] is None else _phase8_json(row[1])))

    def request(self, intent: PublicationEffectIntent) -> tuple[PublicationEffectRecord, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT intent_json, receipt_json FROM publication_effects WHERE effect_id = %s FOR UPDATE", (intent.effect_id,))
            row = cursor.fetchone()
            if row is not None:
                record = self._record(row)
                if record.intent.canonical() != intent.canonical():
                    raise IdempotencyConflictError("publication effect id has conflicting input")
                return record, False
            cursor.execute("SELECT head_sha, deterministic_ref, state, verification_json FROM publications WHERE publication_id = %s FOR UPDATE", (intent.publication_id,))
            publication = cursor.fetchone()
            cursor.execute("SELECT approval_id FROM publication_approvals WHERE publication_id = %s AND head_sha = %s AND decision = 'approved' LIMIT 1", (intent.publication_id, intent.head_sha))
            approval = cursor.fetchone()
            verification = None if publication is None or publication[3] is None else _phase8_json(publication[3])
            cursor.execute("SELECT effect_id, intent_json FROM publication_effects WHERE publication_id = %s AND deterministic_ref = %s FOR UPDATE", (intent.publication_id, intent.deterministic_ref))
            same_ref = cursor.fetchone()
            if same_ref is not None:
                existing_intent = _effect_intent_from_dict(_phase8_json(same_ref[1]))
                if existing_intent.canonical() == intent.canonical():
                    return PublicationEffectRecord(existing_intent), False
                raise IdempotencyConflictError("publication deterministic ref already has an effect intent")
            if publication is None or approval is None or verification is None or publication[0] != intent.head_sha or publication[1] != intent.deterministic_ref or publication[2] != PublicationState.VERIFIED.value or verification.get("status") != VerificationStatus.SUCCEEDED.value:
                raise IdempotencyConflictError("publication effect does not match exact publication head")
            cursor.execute("INSERT INTO publication_effects (effect_id, publication_id, head_sha, deterministic_ref, intent_json, state, schema_version) VALUES (%s, %s, %s, %s, %s::jsonb, 'requested', 1) ON CONFLICT DO NOTHING", (intent.effect_id, intent.publication_id, intent.head_sha, intent.deterministic_ref, intent.canonical()))
            if cursor.rowcount == 0:
                cursor.execute("SELECT intent_json, receipt_json FROM publication_effects WHERE effect_id = %s FOR UPDATE", (intent.effect_id,))
                concurrent = cursor.fetchone()
                if concurrent is None:
                    raise IdempotencyConflictError("publication deterministic ref already has an effect intent")
                record = self._record(concurrent)
                if record.intent.canonical() != intent.canonical():
                    raise IdempotencyConflictError("publication effect id has conflicting input")
                return record, False
        return PublicationEffectRecord(intent), True

    def get(self, effect_id: str) -> PublicationEffectRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT intent_json, receipt_json FROM publication_effects WHERE effect_id = %s", (effect_id,))
            row = cursor.fetchone()
        return None if row is None else self._record(row)

    def reconcile(self, receipt: PublicationReceipt) -> PublicationEffectRecord:
        with self._connection.cursor() as cursor:
            if receipt.state is not PublicationEffectState.RECONCILED:
                raise IdempotencyConflictError("publication receipt must be reconciled")
            cursor.execute("SELECT intent_json, receipt_json FROM publication_effects WHERE effect_id = %s FOR UPDATE", (receipt.effect_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("publication effect does not exist")
            current = self._record(row)
            if current.intent.publication_id != receipt.publication_id:
                raise IdempotencyConflictError("publication effect has conflicting receipt")
            cursor.execute("SELECT base_sha, tree_sha, head_sha FROM publications WHERE publication_id = %s", (receipt.publication_id,))
            publication = cursor.fetchone()
            pull_request = receipt.pull_request
            if publication is None or pull_request is None or pull_request.base_sha != publication[0] or pull_request.tree_sha != publication[1] or pull_request.head_sha != publication[2] or current.intent.head_sha != publication[2]:
                raise IdempotencyConflictError("publication receipt does not match exact prepared head")
            if current.receipt is not None:
                if current.receipt.canonical() != receipt.canonical():
                    raise IdempotencyConflictError("publication effect has conflicting receipt")
                return current
            cursor.execute("UPDATE publication_effects SET receipt_json = %s::jsonb, state = %s, reconciled_at = now() WHERE effect_id = %s", (receipt.canonical(), receipt.state.value, receipt.effect_id))
        return PublicationEffectRecord(current.intent, receipt)


class PostgresCheckRunObservationStore:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def append(self, observation: CheckRunObservation) -> tuple[CheckRunObservation, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT observation_json FROM check_run_observations WHERE publication_id = %s AND head_sha = %s AND check_name = %s AND observed_at = %s FOR UPDATE", (observation.publication_id, observation.head_sha, observation.check_name, observation.observed_at))
            row = cursor.fetchone()
            if row is not None:
                prior = _phase8_json(row[0])
                if _canonical(prior) != observation.canonical():
                    raise IdempotencyConflictError("check observation has conflicting content")
                return observation, False
            cursor.execute("SELECT head_sha FROM publications WHERE publication_id = %s", (observation.publication_id,))
            publication = cursor.fetchone()
            if publication is None or publication[0] != observation.head_sha:
                raise IdempotencyConflictError("check observation does not match exact publication head")
            cursor.execute("INSERT INTO check_run_observations (publication_id, head_sha, check_name, status, conclusion, observed_at, observation_json, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 1) ON CONFLICT (publication_id, head_sha, check_name, observed_at) DO NOTHING", (observation.publication_id, observation.head_sha, observation.check_name, observation.status.value, None if observation.conclusion is None else observation.conclusion.value, observation.observed_at, observation.canonical()))
            if cursor.rowcount == 0:
                cursor.execute("SELECT observation_json FROM check_run_observations WHERE publication_id = %s AND head_sha = %s AND check_name = %s AND observed_at = %s FOR UPDATE", (observation.publication_id, observation.head_sha, observation.check_name, observation.observed_at))
                concurrent = cursor.fetchone()
                if concurrent is None:
                    raise IdempotencyConflictError("check observation conflicts with a concurrent append")
                if _canonical(_phase8_json(concurrent[0])) != observation.canonical():
                    raise IdempotencyConflictError("check observation has conflicting content")
                return observation, False
        return observation, True

    def read(self, publication_id: str, head_sha: str) -> tuple[CheckRunObservation, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT observation_json FROM check_run_observations WHERE publication_id = %s AND head_sha = %s ORDER BY observed_at, observation_id", (publication_id, head_sha))
            rows = cursor.fetchall()
        return tuple(CheckRunObservation(str(data["publication_id"]), str(data["head_sha"]), str(data["check_name"]), CheckRunStatus(str(data["status"])), None if data.get("conclusion") is None else CheckRunConclusion(str(data["conclusion"])), datetime.fromisoformat(str(data["observed_at"])), int(data["schema_version"])) for row in rows for data in (_phase8_json(row[0]),))


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
        self.source_ingestions = PostgresSourceIngestionLedger(self._connection)
        self.control_events = PostgresControlEventStore(self._connection)
        self.artifacts = PostgresArtifactCatalog(self._connection)
        self.runner_sessions = PostgresRunnerSessionStore(self._connection)
        self.worker_results = PostgresWorkerResultLedger(self._connection)
        self.workspaces = PostgresWorkspaceLedger(self._connection)
        self.publications = PostgresPublicationLedger(self._connection)
        self.publication_approvals = PostgresPublicationApprovalStore(self._connection)
        self.publication_effects = PostgresPublicationEffectStore(self._connection)
        self.check_observations = PostgresCheckRunObservationStore(self._connection)
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
