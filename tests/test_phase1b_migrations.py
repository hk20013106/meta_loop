from pathlib import Path


def test_phase1b_migration_has_ledger_and_queue_integrity_constraints():
    sql = (Path(__file__).parents[1] / "migrations" / "0001_phase1b_core.sql").read_text(encoding="utf-8")

    assert "schema_migrations" in sql
    assert "CHECK (sequence > 0)" in sql
    assert "CHECK (attempt >= 0)" in sql
    assert "CHECK (max_attempts > 0)" in sql
    assert "WHERE state = 'ready'" in sql
