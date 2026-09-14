import os
from pathlib import Path

import pytest

DSN = os.environ.get("META_LOOP_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip("BLOCKED_ENVIRONMENT: META_LOOP_TEST_POSTGRES_DSN is not configured", allow_module_level=True)
psycopg = pytest.importorskip("psycopg")

from meta_loop.application.integration_models import CanaryResult, CanaryStatus, IntegrationIntent, IntegrationState, MergeReceipt
from meta_loop.domain.errors import IdempotencyConflictError
from meta_loop.infrastructure.integration_postgres import Phase9PostgresUnitOfWork
from meta_loop.infrastructure.migrations import MigrationRunner

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40


def connection():
    return psycopg.connect(DSN)


def make_intent(identifier="integration-1"):
    return IntegrationIntent(identifier, "publication-1", "effect-1", "task-1", "owner/repository", "main", 17, A, B, C, D, "refs/heads/meta-loop/publication-1", "gov-1", 3, 4)


def test_phase9_migration_exists_and_applies():
    migration = Path(__file__).parents[1] / "migrations" / "0010_phase9_integrations.sql"
    assert migration.exists()
    with connection() as db:
        MigrationRunner.from_directory(migration.parent).apply(db)
        db.commit()
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.integrations'), to_regclass('public.integration_effects'), to_regclass('public.integration_check_observations')")
            assert cursor.fetchone() == ("integrations", "integration_effects", "integration_check_observations")


def test_postgres_integration_ledger_exact_state_and_target_lock():
    with connection() as db:
        MigrationRunner.from_directory(Path(__file__).parents[1] / "migrations").apply(db)
        with db.cursor() as cursor:
            cursor.execute("TRUNCATE integration_check_observations, integration_effects, integrations CASCADE")
        db.commit()

    with Phase9PostgresUnitOfWork(connection) as uow:
        record, created = uow.integrations.reserve(make_intent())
        assert created and record.state is IntegrationState.MERGE_REQUESTED
        with pytest.raises(IdempotencyConflictError):
            uow.integrations.reserve(make_intent("integration-2"))
        merged = uow.integrations.record_merge(MergeReceipt("integration-1", 17, A, C, E, D, A, "main"), record.version)
        pending = uow.integrations.record_canary(CanaryResult("integration-1", E, CanaryStatus.PENDING), merged.version)
        passed = uow.integrations.record_canary(CanaryResult("integration-1", E, CanaryStatus.PASSED), pending.version)
        assert passed.state is IntegrationState.CANARY_PASSED
        uow.commit()

    with Phase9PostgresUnitOfWork(connection) as uow:
        assert uow.integrations.get("integration-1").state is IntegrationState.CANARY_PASSED
        assert uow.integrations.reserve(make_intent("integration-2"))[1]
