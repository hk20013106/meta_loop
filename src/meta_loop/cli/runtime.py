"""Local CLI composition; application services remain driver-independent."""

import os
from pathlib import Path

from meta_loop.application.ingestion import IssueIngestionService
from meta_loop.domain.errors import UnsupportedGovernanceError
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.infrastructure.github import GitHubIssueSource, UrllibJsonTransport
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


def github_issue_source() -> GitHubIssueSource:
    required = {name: os.environ.get(name) for name in ("META_LOOP_GITHUB_ORG", "META_LOOP_GITHUB_REPO", "META_LOOP_GITHUB_PAT", "META_LOOP_TRIGGER_AUTHOR", "META_LOOP_TRIGGER_LABEL")}
    if not all(required.values()):
        raise ValueError("GitHub Issue ingestion is not configured")
    return GitHubIssueSource(required["META_LOOP_GITHUB_ORG"] + "/" + required["META_LOOP_GITHUB_REPO"], required["META_LOOP_TRIGGER_AUTHOR"], required["META_LOOP_TRIGGER_LABEL"], UrllibJsonTransport(required["META_LOOP_GITHUB_PAT"]))


def github_ingestion_service() -> IssueIngestionService:
    root = os.environ.get("META_LOOP_CAS_ROOT")
    if not root:
        raise ValueError("META_LOOP_CAS_ROOT is not configured")
    return IssueIngestionService(FilesystemArtifactStore(root, Path(__file__).parents[3]), __import__("meta_loop.cli.main", fromlist=["SystemClock"]).SystemClock(), __import__("meta_loop.infrastructure.memory", fromlist=["SequentialIdGenerator"]).SequentialIdGenerator("github"))


def doctor() -> dict[str, object]:
    result: dict[str, object] = {"database": "not configured", "migrations": "not checked", "governance": "unavailable", "github": "configured" if all(os.environ.get(name) for name in ("META_LOOP_GITHUB_ORG", "META_LOOP_GITHUB_REPO", "META_LOOP_GITHUB_PAT", "META_LOOP_TRIGGER_AUTHOR", "META_LOOP_TRIGGER_LABEL")) else "not configured"}
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
