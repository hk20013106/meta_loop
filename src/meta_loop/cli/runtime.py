"""Local CLI composition; application services remain driver-independent."""

import os
from pathlib import Path

from meta_loop.domain.errors import UnsupportedGovernanceError
from meta_loop.infrastructure.migrations import MigrationRunner
from meta_loop.infrastructure.postgres import PostgresUnitOfWork


class UnavailableGovernance:
    def read_revision(self, revision: str):
        raise UnsupportedGovernanceError("governance policy is unavailable")

    def authorize(self, revision: str, capability: str) -> bool:
        return False


def dsn() -> str:
    value = os.environ.get("META_LOOP_POSTGRES_DSN")
    if not value:
        raise ValueError("META_LOOP_POSTGRES_DSN is not configured")
    return value


def unit_of_work() -> PostgresUnitOfWork:
    def connect():
        import psycopg
        return psycopg.connect(dsn())
    return PostgresUnitOfWork(connect)


def doctor() -> dict[str, object]:
    result: dict[str, object] = {"database": "not configured", "migrations": "not checked", "governance": "unavailable"}
    try:
        import psycopg
        with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            MigrationRunner.from_directory(Path(__file__).parents[3] / "migrations").validate_applied(tuple(row[0] for row in cursor.fetchall()))
        result["database"] = "connected"
        result["migrations"] = "valid"
    except (ImportError, OSError, ValueError):
        pass
    cas_root = os.environ.get("META_LOOP_CAS_ROOT")
    result["cas"] = "not configured" if not cas_root else ("healthy" if Path(cas_root).is_dir() else "unavailable")
    return result
