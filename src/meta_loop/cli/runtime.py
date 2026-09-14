"""Infrastructure composition for the thin CLI boundary."""

import json
import os
from pathlib import Path

from meta_loop.application.ingestion import IssueIngestionService
from meta_loop.application.models import PublicationIntent, VerificationResultCode, VerificationStatus
from meta_loop.application.publication import PublicationCheckService, PublicationPreparationService, PublicationPublishingService
from meta_loop.domain.errors import UnsupportedGovernanceError, ValidationError
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.infrastructure.github import (
    GitHubPublicationPublisher,
    UrllibJsonTransport,
    github_issue_source_configured,
    github_issue_source_from_environment,
)
from meta_loop.infrastructure.github_checks import GitHubCheckSource
from meta_loop.infrastructure.memory import UUIDIdGenerator
from meta_loop.infrastructure.migrations import MigrationRunner
from meta_loop.infrastructure.postgres import PostgresUnitOfWork
from meta_loop.infrastructure.publication import DockerCandidateVerifier, LocalGitCandidatePreparer


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


def cas_root() -> Path:
    value = os.environ.get("META_LOOP_CAS_ROOT")
    if not value:
        raise ValueError("META_LOOP_CAS_ROOT is not configured")
    return Path(value)


def github_issue_source():
    return github_issue_source_from_environment()


def github_ingestion_service() -> IssueIngestionService:
    revision = os.environ.get("META_LOOP_GOVERNANCE_REVISION")
    if not revision:
        raise ValueError("META_LOOP_GOVERNANCE_REVISION is not configured")
    repository_root = Path(__file__).resolve().parents[3]
    clock = __import__("meta_loop.cli.main", fromlist=["SystemClock"]).SystemClock()
    return IssueIngestionService(
        FilesystemArtifactStore(cas_root(), repository_root),
        clock,
        UUIDIdGenerator(),
        UnavailableGovernance(),
        revision,
    )


def _github_repository_name() -> str:
    org = os.environ.get("META_LOOP_GITHUB_ORG")
    repo = os.environ.get("META_LOOP_GITHUB_REPO")
    if not org or not repo:
        raise ValueError("GitHub repository is not configured")
    return f"{org}/{repo}"


def _publication_paths() -> tuple[Path, Path]:
    repository_root = os.environ.get("META_LOOP_REPOSITORY_ROOT")
    work_root = os.environ.get("META_LOOP_PUBLICATION_WORK_ROOT")
    if not repository_root or not work_root:
        raise ValueError("publication local paths are not configured")
    return Path(repository_root).resolve(), Path(work_root).resolve()


def _github_transport() -> UrllibJsonTransport:
    token = os.environ.get("META_LOOP_GITHUB_PAT")
    if not token:
        raise ValueError("GitHub credentials are not configured")
    return UrllibJsonTransport(token)


def _base_branch() -> str:
    value = os.environ.get("META_LOOP_GITHUB_BASE_BRANCH")
    if not value:
        raise ValueError("GitHub base branch is not configured")
    return value


def _required_checks() -> tuple[str, ...]:
    value = os.environ.get("META_LOOP_REQUIRED_CHECKS")
    if not value:
        raise ValueError("required GitHub checks are not configured")
    checks = tuple(item.strip() for item in value.split(",") if item.strip())
    if not checks or len(set(checks)) != len(checks):
        raise ValueError("required GitHub checks are invalid")
    return checks


def _verifier(repository_name: str, repository_root: Path, work_root: Path) -> DockerCandidateVerifier:
    image = os.environ.get("META_LOOP_VERIFIER_IMAGE")
    raw_argv = os.environ.get("META_LOOP_VERIFIER_ARGV_JSON")
    if not image or not raw_argv:
        raise ValueError("publication verifier is not configured")
    try:
        parsed = json.loads(raw_argv)
    except json.JSONDecodeError as error:
        raise ValueError("publication verifier argv is invalid") from error
    if not isinstance(parsed, list) or not parsed or any(not isinstance(item, str) for item in parsed):
        raise ValueError("publication verifier argv is invalid")
    return DockerCandidateVerifier(
        {repository_name: repository_root},
        work_root,
        image,
        tuple(parsed),
    )


def prepare_publication(publication_id: str, task_id: str, worker_result_id: str, patch_digest: str):
    repository_name = _github_repository_name()
    repository_root, work_root = _publication_paths()
    governance_revision = os.environ.get("META_LOOP_GOVERNANCE_REVISION")
    if not governance_revision:
        raise ValueError("META_LOOP_GOVERNANCE_REVISION is not configured")

    blobs = FilesystemArtifactStore(cas_root(), Path(__file__).resolve().parents[3])
    preparer = LocalGitCandidatePreparer({repository_name: repository_root}, work_root)
    verifier = _verifier(repository_name, repository_root, work_root)
    service = PublicationPreparationService(blobs, preparer, UnavailableGovernance())

    with unit_of_work() as uow:
        task = uow.tasks.get(task_id)
        result = uow.worker_results.get(worker_result_id)
        session = None if result is None else uow.runner_sessions.get(result.session_id)
        if task is None or result is None or session is None:
            raise ValidationError("publication canonical sources are incomplete")
        if task.repository.name != repository_name:
            raise ValidationError("publication task targets a repository outside the configured allowlist")
        intent = PublicationIntent(
            publication_id,
            task_id,
            worker_result_id,
            patch_digest,
            task.repository.name,
            task.repository.revision,
            task.version,
            len(uow.events.read(task_id)),
            session.request.correlation_id,
            governance_revision,
        )
        prepared = service.prepare(uow, intent)
        verification = verifier.verify(intent, prepared)
        record = uow.publications.get(publication_id)
        if record is None:
            raise ValidationError("prepared publication was not recorded")
        uow.publications.record_verification(verification, record.version)
        uow.commit()

    if verification.status is not VerificationStatus.SUCCEEDED or verification.result_code is not VerificationResultCode.PASSED:
        raise ValidationError("publication verification did not pass")
    return prepared


def publication_status(publication_id: str) -> dict:
    effect_id = f"publish:{publication_id}"
    with unit_of_work() as uow:
        record = uow.publications.get(publication_id)
        if record is None:
            raise ValidationError("publication does not exist")
        prepared = record.prepared_head
        verification = record.verification
        effect = uow.publication_effects.get(effect_id)
        approvals = () if prepared is None else uow.publication_approvals.read(publication_id, prepared.head_sha)
    receipt = None if effect is None else effect.receipt
    pull_request = None if receipt is None else receipt.pull_request
    return {
        "publication_id": publication_id,
        "state": record.state.value,
        "version": record.version,
        "base_sha": None if prepared is None else prepared.base_sha,
        "tree_sha": None if prepared is None else prepared.tree_sha,
        "head_sha": None if prepared is None else prepared.head_sha,
        "deterministic_ref": None if prepared is None else prepared.deterministic_ref,
        "verification_status": None if verification is None else verification.status.value,
        "verification_result_code": None if verification is None else verification.result_code.value,
        "approval_count": len(approvals),
        "effect_state": None if receipt is None else receipt.state.value,
        "pull_request_number": None if pull_request is None else pull_request.pull_request_number,
    }


def publish_publication(publication_id: str):
    repository_name = _github_repository_name()
    repository_root, _ = _publication_paths()
    publisher = GitHubPublicationPublisher(
        {repository_name: repository_root},
        {repository_name: _base_branch()},
        _github_transport(),
        enabled=os.environ.get("META_LOOP_GITHUB_WRITE_ENABLED") == "1",
    )
    return PublicationPublishingService(unit_of_work, publisher).publish(
        publication_id,
        f"publish:{publication_id}",
    )


def check_publication(publication_id: str):
    source = GitHubCheckSource(_github_transport())
    return PublicationCheckService(unit_of_work, source, _required_checks()).check(
        publication_id,
        f"publish:{publication_id}",
    )


def doctor() -> dict[str, object]:
    result: dict[str, object] = {
        "database": "not configured",
        "migrations": "not checked",
        "governance": "unavailable",
        "github": "configured" if github_issue_source_configured() else "not configured",
    }
    try:
        import psycopg
        with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            MigrationRunner.from_directory(Path(__file__).resolve().parents[3] / "migrations").validate_applied(tuple(row[0] for row in cursor.fetchall()))
        result["database"] = "connected"
        result["migrations"] = "valid"
    except (ImportError, OSError, ValueError):
        pass
    root = os.environ.get("META_LOOP_CAS_ROOT")
    result["cas"] = "not configured" if not root else ("healthy" if Path(root).is_dir() else "unavailable")
    return result
