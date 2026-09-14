import pytest

from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.migrations import MigrationRunner


def test_migration_runner_orders_files_and_calculates_stable_checksums():
    runner = MigrationRunner.from_directory("migrations")

    migrations = runner.plan()

    assert [migration.version for migration in migrations] == ["0001_phase1b_core", "0002_phase2_control", "0003_phase3_artifacts", "0004_phase4_runner_sessions", "0005_phase5_worker_results", "0006_phase6_workspaces", "0007_phase7_issue_ingestion", "0008_phase8_publications", "0009_phase8_github_branch_refs", "0010_phase9_integrations"]
    assert len(migrations[0].checksum) == 64


def test_migration_runner_rejects_unknown_or_out_of_order_ledger_entries():
    runner = MigrationRunner.from_directory("migrations")

    with pytest.raises(ValidationError):
        runner.validate_applied(("0002_future",))
