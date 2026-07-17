"""Phase 8 durable ledger integration; requires a disposable PostgreSQL database."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


DSN = os.environ.get("META_LOOP_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip("BLOCKED_ENVIRONMENT: META_LOOP_TEST_POSTGRES_DSN is not configured", allow_module_level=True)
psycopg = pytest.importorskip("psycopg")

from meta_loop.application.models import (
    CatalogedArtifact,
    CheckRunObservation,
    PublicationEffectIntent,
    PublicationEffectState,
    PublicationIntent,
    PublicationReceipt,
    PublicationReviewDecision,
    PullRequestReceipt,
    PreparedHead,
    ReviewDecision,
    RunnerSessionRequest,
    RunnerSessionOutcome,
    RunnerSessionState,
    VerificationResult,
    VerificationResultCode,
    VerifierProfile,
    CheckRunConclusion,
    CheckRunStatus,
    VerificationStatus,
    WorkerIdentity,
    WorkerResult,
)
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.migrations import MigrationRunner
from meta_loop.infrastructure.postgres import PostgresUnitOfWork


NOW = datetime(2026, 7, 18, tzinfo=UTC)


def connection():
    return psycopg.connect(DSN)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    with connection() as db:
        MigrationRunner.from_directory(Path(__file__).parents[1] / "migrations").apply(db)
        db.commit()


@pytest.fixture(autouse=True)
def isolate_disposable_database():
    with connection() as db, db.cursor() as cursor:
        cursor.execute("TRUNCATE check_run_observations, publication_effects, publication_approvals, publications, source_ingestions, workspace_allocations, worker_results, runner_sessions, artifact_catalog, task_events, task_queue, tasks CASCADE")
        cursor.execute("UPDATE system_fuse SET engaged = FALSE, changed_at = now(), governance_revision = NULL WHERE singleton = TRUE")
        db.commit()


def _intent() -> PublicationIntent:
    return PublicationIntent("publication-1", "task-1", "result-1", "b" * 64, "owner/repository", "c" * 40, 1, 0, "correlation-1", "governance-1")


def _prepared() -> PreparedHead:
    return PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, "refs/meta-loop/publication-1")


def _seed(uow):
    patch = ArtifactRef(ArtifactDigest("b" * 64), "change.diff", "text/x-diff")
    review = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")
    initial = Task.create("task-1", TaskSource("synthetic", "fixture"), RepositoryTarget("owner/repository", "c" * 40), RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1), NOW)
    task = replace(initial.attach_artifact(patch), status=TaskStatus.READY_TO_PUBLISH)
    uow.tasks.create(initial)
    uow.tasks.update(task, 0)
    uow.artifacts.register(CatalogedArtifact(patch, "internal", "worker_result:result-1", 10))
    uow.artifacts.register(CatalogedArtifact(review, "internal", "review", 10))
    uow.runner_sessions.record_once(RunnerSessionRequest("session-1", "task-1", Role.IMPLEMENTER, 1, 0, (), 60, "session-correlation"))
    uow.runner_sessions.set_outcome(RunnerSessionOutcome("session-1", RunnerSessionState.SUCCEEDED, "ready"))
    uow.runner_sessions.record_once(RunnerSessionRequest("review-session-1", "task-1", Role.PATCH_REVIEWER, 0, 0, (), 60, "review-correlation"))
    uow.runner_sessions.set_outcome(RunnerSessionOutcome("review-session-1", RunnerSessionState.SUCCEEDED, "approved"))
    uow.worker_results.record_once(WorkerResult("result-1", "session-1", "task-1", WorkerIdentity("worker-1", Role.IMPLEMENTER), TaskStatus.READY_TO_PUBLISH, "ready", 1, 0))
    uow.worker_results.record_once(WorkerResult("review-result-1", "review-session-1", "task-1", WorkerIdentity("reviewer-1", Role.PATCH_REVIEWER), TaskStatus.READY_TO_PUBLISH, "approved", 0, 0))
    return review


def test_postgres_publication_ledger_is_atomic_append_only_and_exact_head():
    with PostgresUnitOfWork(connection) as uow:
        review_artifact = _seed(uow)
        record, created = uow.publications.reserve(_intent())
        assert created and record.version == 0
        prepared = uow.publications.record_prepared(_prepared(), 0)
        verification = uow.publications.record_verification(VerificationResult("publication-1", "e" * 40, "sha256:" + "f" * 64, VerifierProfile.DEFAULT, VerificationStatus.SUCCEEDED, VerificationResultCode.PASSED), prepared.version)
        decision = PublicationReviewDecision("approval-1", "publication-1", "task-1", "review-session-1", "review-result-1", review_artifact, "b" * 64, "c" * 40, "d" * 40, "e" * 40, "reviewer-1", ReviewDecision.APPROVED, NOW)
        assert uow.publication_approvals.append(decision)[1]
        effect, created = uow.publication_effects.request(PublicationEffectIntent("effect-1", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))
        assert created and effect.receipt is None
        assert uow.check_observations.append(CheckRunObservation("publication-1", "e" * 40, "lint", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS, NOW))[1]
        uow.commit()

    with PostgresUnitOfWork(connection) as uow:
        assert not uow.publications.reserve(_intent())[1]
        assert uow.publication_approvals.read("publication-1", "e" * 40)[0].approval_id == "approval-1"
        assert uow.check_observations.read("publication-1", "e" * 40)[0].check_name == "lint"
        with pytest.raises(OptimisticConflictError):
            uow.publications.record_prepared(_prepared(), 0)
        receipt = PublicationReceipt("publication-1", "effect-1", PublicationEffectState.RECONCILED, PullRequestReceipt("publication-1", 1, "c" * 40, "d" * 40, "e" * 40))
        assert uow.publication_effects.reconcile(receipt).receipt == receipt
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.reconcile(PublicationReceipt("publication-1", "effect-1", PublicationEffectState.REQUESTED))
        uow.commit()

    with connection() as db, db.cursor() as cursor:
        with pytest.raises(Exception):
            cursor.execute("DELETE FROM publication_approvals WHERE approval_id = 'approval-1'")
        db.rollback()


def test_postgres_effect_rejects_unapproved_duplicate_and_mismatched_receipt():
    with PostgresUnitOfWork(connection) as uow:
        review_artifact = _seed(uow)
        uow.publications.reserve(_intent())
        prepared = uow.publications.record_prepared(_prepared(), 0)
        uow.publications.record_verification(VerificationResult("publication-1", "e" * 40, "sha256:" + "f" * 64, VerifierProfile.DEFAULT, VerificationStatus.SUCCEEDED, VerificationResultCode.PASSED), prepared.version)
        effect = PublicationEffectIntent("effect-1", "publication-1", "e" * 40, "refs/meta-loop/publication-1")
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.request(effect)
        decision = PublicationReviewDecision("approval-1", "publication-1", "task-1", "review-session-1", "review-result-1", review_artifact, "b" * 64, "c" * 40, "d" * 40, "e" * 40, "reviewer-1", ReviewDecision.APPROVED, NOW)
        uow.publication_approvals.append(decision)
        assert uow.publication_effects.request(effect)[1]
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.request(PublicationEffectIntent("effect-2", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.reconcile(PublicationReceipt("publication-1", "effect-1", PublicationEffectState.RECONCILED, PullRequestReceipt("publication-1", 1, "c" * 40, "0" * 40, "e" * 40)))
