from pathlib import Path


def test_phase1b_migration_has_ledger_and_queue_integrity_constraints():
    sql = (Path(__file__).parents[1] / "migrations" / "0001_phase1b_core.sql").read_text(encoding="utf-8")

    assert "schema_migrations" in sql
    assert "CHECK (sequence > 0)" in sql
    assert "CHECK (attempt >= 0)" in sql
    assert "CHECK (max_attempts > 0)" in sql
    assert "WHERE state = 'ready'" in sql


def test_phase6_workspace_migration_has_schema_idempotency_and_active_task_purpose_constraints():
    sql = (Path(__file__).parents[1] / "migrations" / "0006_phase6_workspaces.sql").read_text(encoding="utf-8")

    assert "workspace_allocations" in sql
    assert "schema_version = 1" in sql
    assert "UNIQUE (allocation_id)" in sql
    assert "WHERE state = 'active'" in sql
