"""Real integration tests; require an explicitly provisioned disposable database."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest


DSN = os.environ.get("META_LOOP_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip("BLOCKED_ENVIRONMENT: META_LOOP_TEST_POSTGRES_DSN is not configured", allow_module_level=True)
psycopg = pytest.importorskip("psycopg")

from meta_loop.application.models import QueueEnqueueResult
from meta_loop.application.controller import FuseService
from meta_loop.application.ingestion import IssueCandidate, IssueIngestionService, IssueTaskSpec
from meta_loop.application.runners import RunnerSessionRequest, RunnerSessionService
from meta_loop.application.models import WorkspacePurpose, WorkspaceRequest
from meta_loop.application.workspaces import FakeWorkspaceManager, WorkspaceAllocationService
from meta_loop.application.services import TaskQueueService
from meta_loop.infrastructure.memory import FixedClock, SequentialIdGenerator
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, LeaseLostError, OptimisticConflictError, SequenceConflictError, ValidationError
from meta_loop.domain.events import Event
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.migrations import MigrationRunner
from meta_loop.infrastructure.postgres import PostgresUnitOfWork


NOW = datetime(2026, 7, 13, tzinfo=UTC)


class AllowIssueIngestionGovernance:
    def read_revision(self, revision):
        assert revision == "gov-7"

    def authorize(self, revision, capability):
        return revision == "gov-7" and capability == "github_issue_ingestion"


def connection():
    return psycopg.connect(DSN)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    with connection() as db:
        MigrationRunner.from_directory(Path(__file__).parents[1] / "migrations").apply(db)
        db.commit()


@pytest.fixture(autouse=True)
def isolate_disposable_database():
    """The disposable test user is privileged; runtime adapters never truncate events."""
    with connection() as db, db.cursor() as cursor:
        reset_disposable_database(cursor)
        db.commit()


def reset_disposable_database(cursor):
    cursor.execute("TRUNCATE source_ingestions, workspace_allocations, task_events, task_queue, tasks CASCADE")
    cursor.execute("UPDATE system_fuse SET engaged = FALSE, changed_at = now(), governance_revision = NULL WHERE singleton = TRUE")


def make_task() -> Task:
    task_id = f"task-{uuid4()}"
    return Task.create(task_id, TaskSource("synthetic", task_id), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), NOW)


def create_task(task: Task) -> None:
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        uow.commit()


def test_migrations_are_repeatable_only_with_same_ledger_checksum():
    with connection() as db:
        MigrationRunner.from_directory(Path(__file__).parents[1] / "migrations").apply(db)
        db.commit()


def test_migration_runner_rejects_an_out_of_order_ledger_entry():
    runner = MigrationRunner.from_directory(Path(__file__).parents[1] / "migrations")
    with connection() as db, db.cursor() as cursor:
        cursor.execute("INSERT INTO schema_migrations (version, checksum) VALUES ('9999_out_of_order', 'synthetic')")
        db.commit()
        try:
            with pytest.raises(ValidationError):
                runner.apply(db)
        finally:
            db.rollback()
            cursor.execute("DELETE FROM schema_migrations WHERE version = '9999_out_of_order'")
            db.commit()


def test_task_round_trip_and_optimistic_conflict():
    task = make_task()
    create_task(task)
    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id) == task
        with pytest.raises(OptimisticConflictError):
            uow.tasks.update(task.transition(TaskStatus.VALIDATED, Role.VALIDATOR, NOW), expected_version=1)


def test_events_are_idempotent_and_sequence_is_checked():
    task = make_task()
    create_task(task)
    event = Event(f"event-{uuid4()}", task.task_id, EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {}, 1, None, "c", 1)
    with PostgresUnitOfWork(connection) as uow:
        assert uow.events.append(event, 0) == event
        assert uow.events.append(event, 1) == event
        with pytest.raises(SequenceConflictError):
            uow.events.append(Event(f"event-{uuid4()}", task.task_id, EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {}, 1, None, "c", 1), 1)
        with pytest.raises(IdempotencyConflictError):
            uow.events.append(Event(event.event_id, task.task_id, EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {"changed": True}, 1, None, "c", 2), 1)
        uow.commit()


def test_two_open_transactions_cannot_claim_same_task():
    task = make_task()
    create_task(task)
    with PostgresUnitOfWork(connection) as first, PostgresUnitOfWork(connection) as second:
        first.queue.enqueue(task.task_id, 0, NOW, "enqueue-1")
        first.commit()
        assert first.queue.claim("worker-a", NOW, timedelta(seconds=300)) is not None
        assert second.queue.claim("worker-b", NOW, timedelta(seconds=300)) is None
        first.commit()
        second.commit()


def test_lease_owner_is_enforced_and_expiry_recovers():
    task = make_task()
    create_task(task)
    with PostgresUnitOfWork(connection) as uow:
        assert isinstance(uow.queue.enqueue(task.task_id, 0, NOW, "enqueue-1"), QueueEnqueueResult)
        lease = uow.queue.claim("owner", NOW, timedelta(seconds=1))
        with pytest.raises(LeaseLostError):
            uow.queue.complete(task.task_id, "other", NOW)
        with pytest.raises(LeaseLostError):
            uow.queue.complete(task.task_id, "owner", NOW + timedelta(seconds=2))
        assert len(uow.queue.recover_expired(NOW + timedelta(seconds=2))) == 1
        uow.commit()


def test_event_table_rejects_update_and_delete():
    task = make_task()
    create_task(task)
    event = Event(f"event-{uuid4()}", task.task_id, EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {}, 1, None, "c", 1)
    with PostgresUnitOfWork(connection) as uow:
        uow.events.append(event, 0)
        uow.commit()
    with connection() as db, db.cursor() as cursor:
        with pytest.raises(Exception):
            cursor.execute("DELETE FROM task_events WHERE event_id = %s", (event.event_id,))
        with pytest.raises(Exception):
            cursor.execute("UPDATE task_events SET actor = 'system' WHERE event_id = %s", (event.event_id,))


def test_two_connections_cannot_append_the_same_next_event_sequence():
    task = make_task()
    create_task(task)

    def append(event_id: str) -> str:
        try:
            with PostgresUnitOfWork(connection) as uow:
                uow.events.append(Event(event_id, task.task_id, EventType.TASK_RECEIVED, NOW, Role.SYSTEM, {}, 1, None, "c", 1), 0)
                uow.commit()
                return "appended"
        except SequenceConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, (f"event-{uuid4()}", f"event-{uuid4()}")))

    assert sorted(results) == ["appended", "conflict"]


def test_skip_locked_allows_two_workers_to_claim_two_tasks():
    first, second = make_task(), make_task()
    create_task(first)
    create_task(second)
    with PostgresUnitOfWork(connection) as uow:
        uow.queue.enqueue(first.task_id, 0, NOW, "enqueue-1")
        uow.queue.enqueue(second.task_id, 0, NOW, "enqueue-2")
        uow.commit()

    def claim(worker: str) -> str:
        with PostgresUnitOfWork(connection) as uow:
            lease = uow.queue.claim(worker, NOW, timedelta(seconds=300))
            uow.commit()
            return lease.task_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ("worker-a", "worker-b")))

    assert set(claimed) == {first.task_id, second.task_id}


def test_enqueue_is_idempotent_only_for_identical_intent_and_heartbeat_extends_lease():
    task = make_task()
    create_task(task)
    with PostgresUnitOfWork(connection) as uow:
        assert uow.queue.enqueue(task.task_id, 2, NOW, "enqueue-1").created
        assert not uow.queue.enqueue(task.task_id, 2, NOW, "enqueue-1").created
        with pytest.raises(IdempotencyConflictError):
            uow.queue.enqueue(task.task_id, 1, NOW, "enqueue-2")
        lease = uow.queue.claim("owner", NOW, timedelta(seconds=10))
        extended = uow.queue.heartbeat(task.task_id, "owner", NOW + timedelta(seconds=1), timedelta(seconds=60))
        assert extended.expires_at > lease.expires_at
        uow.commit()


def test_uncommitted_uow_rolls_back_task_and_queue_changes():
    task = make_task()
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        uow.queue.enqueue(task.task_id, 0, NOW, "enqueue-1")

    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id) is None


def test_max_attempt_failure_atomically_marks_task_failed_and_records_event():
    task = make_task()
    create_task(task)
    service = TaskQueueService()
    current_time = NOW
    with PostgresUnitOfWork(connection) as uow:
        uow.queue.enqueue(task.task_id, 0, current_time, "enqueue-1")
        for attempt in range(1, 4):
            lease = uow.queue.claim("worker", current_time, timedelta(seconds=30))
            assert lease is not None and lease.expires_at > current_time
            event = Event(f"event-{uuid4()}", task.task_id, EventType.TASK_TRANSITIONED, current_time, Role.SYSTEM, {"attempt": attempt}, 1, None, "c", attempt)
            result = service.fail(uow, task.task_id, "worker", current_time, "synthetic failure", event, attempt - 1)
            if not result.terminal:
                current_time = result.retry_at
        uow.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id).status is TaskStatus.FAILED
        assert len(uow.events.read(task.task_id)) == 3
        assert uow.queue.claim("another", current_time, timedelta(seconds=30)) is None


def test_expired_max_attempt_is_terminalized_atomically_by_service():
    task = make_task()
    create_task(task)
    with PostgresUnitOfWork(connection) as uow:
        uow.queue.enqueue(task.task_id, 0, NOW, "enqueue-1")
        current_time = NOW
        for _ in range(2):
            uow.queue.claim("worker", current_time, timedelta(seconds=30))
            current_time = uow.queue.fail(task.task_id, "worker", current_time, "synthetic failure").retry_at
        uow.queue.claim("worker", current_time, timedelta(seconds=1))
        event = Event(f"event-{uuid4()}", task.task_id, EventType.TASK_TRANSITIONED, current_time, Role.SYSTEM, {}, 1, None, "c", 1)
        result = TaskQueueService().recover_expired(uow, current_time + timedelta(seconds=2), {task.task_id: (event, 0)})
        assert len(result) == 1
        assert result[0].terminal
        uow.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id).status is TaskStatus.FAILED
        assert uow.events.read(task.task_id) == (event,)


def test_runner_session_ledger_is_idempotent_and_task_event_atomic():
    task = make_task()
    request = RunnerSessionRequest("session-" + task.task_id, task.task_id, Role.PLANNER, 0, 0, (), 60, "runner-correlation")
    service = RunnerSessionService(FixedClock(NOW), SequentialIdGenerator("event"))
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        assert service.request(uow, request).created
        uow.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert not service.request(uow, request).created
        with pytest.raises(IdempotencyConflictError):
            service.request(uow, RunnerSessionRequest(request.session_id, task.task_id, Role.PLANNER, 0, 0, (), 61, "runner-correlation"))
        assert len(uow.events.read(task.task_id)) == 1


def test_uncommitted_runner_session_request_rolls_back_with_task_and_event():
    task = make_task()
    request = RunnerSessionRequest("session-" + task.task_id, task.task_id, Role.PLANNER, 0, 0, (), 60, "runner-correlation")
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        RunnerSessionService(FixedClock(NOW), SequentialIdGenerator("event")).request(uow, request)

    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id) is None
        assert uow.runner_sessions.get(request.session_id) is None


def test_workspace_ledger_and_task_event_share_a_postgres_uow_boundary():
    task = make_task()
    request = WorkspaceRequest("workspace-" + task.task_id, task.task_id, Role.PLANNER, WorkspacePurpose.PLANNING, "a" * 40, 0, 0, "workspace-correlation")
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        assert service.allocate(uow, request).created
        uow.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert not service.allocate(uow, request).created
        assert uow.workspaces.get(request.allocation_id).read_only
        assert uow.events.read(task.task_id)[0].event_type is EventType.WORKSPACE_ALLOCATED


def test_uncommitted_workspace_allocation_rolls_back_ledger_and_event():
    task = make_task()
    request = WorkspaceRequest("workspace-" + task.task_id, task.task_id, Role.PLANNER, WorkspacePurpose.PLANNING, "a" * 40, 0, 0, "workspace-correlation")
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager()).allocate(uow, request)

    with PostgresUnitOfWork(connection) as uow:
        assert uow.tasks.get(task.task_id) is None
        assert uow.workspaces.get(request.allocation_id) is None


def test_issue_ingestion_ledger_task_artifact_and_events_share_a_postgres_uow(tmp_path):
    candidate = IssueCandidate("github", "github:repository:17", "owner/repository#17", "event-17", "kai", "meta-loop:ready", NOW, IssueTaskSpec("Update", ("tests pass",), "a" * 40, RiskClass.R1))
    service = IssueIngestionService(FilesystemArtifactStore(tmp_path / "cas"), FixedClock(NOW), SequentialIdGenerator("phase7"), AllowIssueIngestionGovernance(), "gov-7")
    with PostgresUnitOfWork(connection) as uow:
        receipt = service.ingest(uow, candidate)
        uow.commit()
    with PostgresUnitOfWork(connection) as uow:
        assert not service.ingest(uow, candidate).created
        assert uow.source_ingestions.get(candidate.source_key).task_id == receipt.task_id
        assert len(uow.events.read(receipt.task_id)) == 3

    rollback = IssueCandidate("github", "github:repository:18", "owner/repository#18", "event-18", "kai", "meta-loop:ready", NOW, IssueTaskSpec("Rollback", ("tests pass",), "b" * 40, RiskClass.R1))
    with PostgresUnitOfWork(connection) as uow:
        service.ingest(uow, rollback)
    with PostgresUnitOfWork(connection) as uow:
        assert uow.source_ingestions.get(rollback.source_key) is None


def test_same_source_concurrent_ingestion_serializes_to_duplicate_and_preserves_drift(tmp_path):
    candidate = IssueCandidate("github", "github:repository:19", "owner/repository#19", "event-19", "kai", "meta-loop:ready", NOW, IssueTaskSpec("Update", ("tests pass",), "a" * 40, RiskClass.R1))

    def ingest() -> object:
        service = IssueIngestionService(FilesystemArtifactStore(tmp_path / "cas"), FixedClock(NOW), SequentialIdGenerator("phase7"), AllowIssueIngestionGovernance(), "gov-7")
        with PostgresUnitOfWork(connection) as uow:
            receipt = service.ingest(uow, candidate)
            uow.commit()
            return receipt

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda _: ingest(), range(2)))

    assert {receipt.task_id for receipt in receipts} == {receipts[0].task_id}
    assert sorted(receipt.created for receipt in receipts) == [False, True]
    changed = IssueCandidate("github", candidate.source_key, candidate.source_reference, candidate.trigger_event_id, candidate.trigger_actor, candidate.trigger_label, candidate.occurred_at, IssueTaskSpec("Changed", ("tests pass",), "a" * 40, RiskClass.R1))
    with PostgresUnitOfWork(connection) as uow, pytest.raises(IdempotencyConflictError, match="source"):
        IssueIngestionService(FilesystemArtifactStore(tmp_path / "cas"), FixedClock(NOW), SequentialIdGenerator("phase7"), AllowIssueIngestionGovernance(), "gov-7").ingest(uow, changed)


def test_same_trigger_concurrent_ingestion_rejects_before_second_task_creation(tmp_path):
    first = IssueCandidate("github", "github:repository:20", "owner/repository#20", "event-shared", "kai", "meta-loop:ready", NOW, IssueTaskSpec("First", ("tests pass",), "a" * 40, RiskClass.R1))
    second = IssueCandidate("github", "github:repository:21", "owner/repository#21", "event-shared", "kai", "meta-loop:ready", NOW, IssueTaskSpec("Second", ("tests pass",), "a" * 40, RiskClass.R1))

    def ingest(candidate) -> str:
        try:
            with PostgresUnitOfWork(connection) as uow:
                IssueIngestionService(FilesystemArtifactStore(tmp_path / "cas"), FixedClock(NOW), SequentialIdGenerator("phase7"), AllowIssueIngestionGovernance(), "gov-7").ingest(uow, candidate)
                uow.commit()
            return "created"
        except IdempotencyConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(ingest, (first, second))) == ["conflict", "created"]
    with PostgresUnitOfWork(connection) as uow:
        assert len(uow.tasks.list()) == 1


def test_committed_fuse_engage_blocks_interleaved_issue_ingestion(tmp_path):
    candidate = IssueCandidate("github", "github:repository:22", "owner/repository#22", "event-22", "kai", "meta-loop:ready", NOW, IssueTaskSpec("Blocked", ("tests pass",), "a" * 40, RiskClass.R1))
    barrier = Barrier(2)

    def engage() -> None:
        with PostgresUnitOfWork(connection) as uow:
            FuseService().engage(uow, NOW)
            barrier.wait()
            uow.commit()

    def ingest() -> str:
        barrier.wait()
        try:
            with PostgresUnitOfWork(connection) as uow:
                IssueIngestionService(FilesystemArtifactStore(tmp_path / "cas"), FixedClock(NOW), SequentialIdGenerator("phase7"), AllowIssueIngestionGovernance(), "gov-7").ingest(uow, candidate)
                uow.commit()
        except ValidationError:
            return "blocked"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert set(executor.map(lambda fn: fn(), (engage, ingest))) == {None, "blocked"}


def test_postgres_test_isolation_resets_fuse_state():
    with connection() as db, db.cursor() as cursor:
        cursor.execute("UPDATE system_fuse SET engaged = TRUE WHERE singleton = TRUE")
        reset_disposable_database(cursor)
        db.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert not uow.fuse.get().engaged


def test_postgres_workspace_ledger_rejects_another_active_task_purpose():
    task = make_task()
    first = WorkspaceRequest("workspace-a-" + task.task_id, task.task_id, Role.PLANNER, WorkspacePurpose.PLANNING, "a" * 40, 0, 0, "workspace-a")
    second = WorkspaceRequest("workspace-b-" + task.task_id, task.task_id, Role.PLANNER, WorkspacePurpose.PLANNING, "a" * 40, 0, 1, "workspace-b")
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with PostgresUnitOfWork(connection) as uow:
        uow.tasks.create(task)
        service.allocate(uow, first)
        with pytest.raises(IdempotencyConflictError):
            service.allocate(uow, second)
