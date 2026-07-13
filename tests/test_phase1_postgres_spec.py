from pathlib import Path


def test_phase1b_migration_declares_skip_locked_and_append_only_event_guards():
    migration = (Path(__file__).parents[1] / "migrations" / "0001_phase1b_core.sql").read_text(encoding="utf-8")

    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "UNIQUE (task_id, sequence)" in migration
    assert "BEFORE UPDATE OR DELETE" in migration
