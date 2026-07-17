from datetime import UTC, datetime
from pathlib import Path
import re

import pytest

from meta_loop.application import models
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


def test_phase8_publication_contracts_are_public_schema_v1_models():
    assert hasattr(models, "PublicationIntent")
    assert hasattr(models, "PreparedHead")
    assert hasattr(models, "VerificationResult")
    assert hasattr(models, "PublicationReviewDecision")
    assert hasattr(models, "PullRequestReceipt")
    assert hasattr(models, "CheckRunObservation")
    assert hasattr(models, "PublicationReceipt")
    assert hasattr(models, "CheckGateResult")


def test_phase8_contracts_are_canonical_and_reject_unsafe_identifiers():
    now = datetime(2026, 7, 18, tzinfo=UTC)
    artifact = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")
    intent = models.PublicationIntent(
        "publication-1", "task-1", "result-1", "b" * 64, "owner/repository", "c" * 40,
        4, 7, "correlation-1", "governance-1",
    )
    prepared = models.PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, "refs/meta-loop/publication-1")
    verification = models.VerificationResult(
        "publication-1", "e" * 40, "sha256:" + "f" * 64, models.VerifierProfile.DEFAULT,
        models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED,
    )
    decision = models.PublicationReviewDecision(
        "approval-1", "publication-1", "task-1", "session-1", "result-2", artifact,
        "b" * 64, "c" * 40, "d" * 40, "e" * 40, "reviewer-1",
        models.ReviewDecision.APPROVED, now,
    )
    observation = models.CheckRunObservation(
        "publication-1", "e" * 40, "lint", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS, now,
    )

    for value in (intent, prepared, verification, decision, observation):
        canonical = value.canonical()
        assert '"schema_version":1' in canonical
        for forbidden in ("path", "command", "argv", "environment", "token", "dsn", "blob", "output"):
            assert forbidden not in canonical

    with pytest.raises(ValidationError):
        models.PreparedHead("publication-1", "b" * 64, "C" * 40, "d" * 40, "e" * 40, "refs/meta-loop/publication-1")


def _intent() -> models.PublicationIntent:
    return models.PublicationIntent(
        "publication-1", "task-1", "result-1", "b" * 64, "owner/repository", "c" * 40,
        1, 7, "correlation-1", "governance-1",
    )


def _prepared() -> models.PreparedHead:
    return models.PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, "refs/meta-loop/publication-1")


def _seed_publication_references(uow: InMemoryUnitOfWork, now: datetime) -> None:
    artifact = ArtifactRef(ArtifactDigest("b" * 64), "change.diff", "text/x-diff")
    initial_task = Task.create("task-1", TaskSource("synthetic", "fixture"), RepositoryTarget("owner/repository", "c" * 40), RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1), now)
    task = Task("task-1", initial_task.source, initial_task.repository, initial_task.risk, now, TaskStatus.READY_TO_PUBLISH, 1, 0, (artifact,))
    result = models.WorkerResult(
        "result-1", "session-1", "task-1", models.WorkerIdentity("worker-1", Role.IMPLEMENTER),
        TaskStatus.READY_TO_PUBLISH, "ready", 1, 7,
    )
    uow.tasks.create(initial_task)
    uow.tasks.update(task, 0)
    uow.artifacts.register(models.CatalogedArtifact(artifact, "internal", "worker_result:result-1", 10))
    uow.runner_sessions.record_once(models.RunnerSessionRequest("session-1", "task-1", Role.IMPLEMENTER, 1, 7, (), 60, "implementer-correlation"))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("session-1", models.RunnerSessionState.SUCCEEDED, "ready"))
    uow.worker_results.record_once(result)


def test_memory_publication_ledger_keeps_canonical_input_and_optimistic_state():
    now = datetime(2026, 7, 18, tzinfo=UTC)
    uow = InMemoryUnitOfWork()
    with uow:
        _seed_publication_references(uow, now)
        record, created = uow.publications.reserve(_intent())
        assert created
        assert record.intent == _intent()
        assert record.state is models.PublicationState.RESERVED
        assert record.version == 0
        assert not uow.publications.reserve(_intent())[1]
        with pytest.raises(IdempotencyConflictError):
            uow.publications.reserve(models.PublicationIntent(
                "publication-1", "task-1", "result-1", "b" * 64, "owner/repository", "c" * 40,
                4, 7, "different-correlation", "governance-1",
            ))
        prepared = uow.publications.record_prepared(_prepared(), expected_version=0)
        assert prepared.version == 1
        assert prepared.state is models.PublicationState.PREPARED
        with pytest.raises(OptimisticConflictError):
            uow.publications.record_prepared(_prepared(), expected_version=0)


def test_memory_publication_fact_stores_are_append_only_and_exact_head():
    now = datetime(2026, 7, 18, tzinfo=UTC)
    approval_artifact = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")
    decision = models.PublicationReviewDecision(
        "approval-1", "publication-1", "task-1", "review-session", "review-result", approval_artifact,
        "b" * 64, "c" * 40, "d" * 40, "e" * 40, "reviewer-1", models.ReviewDecision.APPROVED, now,
    )
    observation = models.CheckRunObservation("publication-1", "e" * 40, "lint", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS, now)
    uow = InMemoryUnitOfWork()
    with uow:
        _seed_publication_references(uow, now)
        uow.artifacts.register(models.CatalogedArtifact(approval_artifact, "internal", "review", 10))
        uow.runner_sessions.record_once(models.RunnerSessionRequest("review-session", "task-1", Role.PATCH_REVIEWER, 0, 0, (), 60, "review-correlation"))
        uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("review-session", models.RunnerSessionState.SUCCEEDED, "approved"))
        uow.worker_results.record_once(models.WorkerResult("review-result", "review-session", "task-1", models.WorkerIdentity("reviewer-1", Role.PATCH_REVIEWER), TaskStatus.READY_TO_PUBLISH, "approved", 0, 0))
        uow.publications.reserve(_intent())
        uow.publications.record_prepared(_prepared(), 0)
        uow.publications.record_verification(models.VerificationResult("publication-1", "e" * 40, "sha256:" + "f" * 64, models.VerifierProfile.DEFAULT, models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED), 1)
        assert uow.publication_approvals.append(decision)[1]
        assert not uow.publication_approvals.append(decision)[1]
        with pytest.raises(IdempotencyConflictError):
            uow.publication_approvals.append(models.PublicationReviewDecision(
                "approval-1", "publication-1", "task-1", "review-session", "review-result", approval_artifact,
                "b" * 64, "c" * 40, "d" * 40, "f" * 40, "reviewer-1", models.ReviewDecision.APPROVED, now,
            ))
        assert uow.check_observations.append(observation)[1]
        assert uow.check_observations.read("publication-1", "e" * 40) == (observation,)
        assert uow.check_observations.read("publication-1", "f" * 40) == ()
        with pytest.raises(IdempotencyConflictError):
            uow.check_observations.append(models.CheckRunObservation("publication-1", "f" * 40, "lint", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS, now))


def test_memory_approval_and_effect_require_successful_exact_head_facts():
    now = datetime(2026, 7, 18, tzinfo=UTC)
    uow = InMemoryUnitOfWork()
    approval_artifact = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")
    decision = models.PublicationReviewDecision("approval-guard", "publication-1", "task-1", "review-session", "review-result", approval_artifact, "b" * 64, "c" * 40, "d" * 40, "e" * 40, "reviewer-1", models.ReviewDecision.APPROVED, now)
    with uow:
        _seed_publication_references(uow, now)
        uow.artifacts.register(models.CatalogedArtifact(approval_artifact, "internal", "review", 10))
        uow.runner_sessions.record_once(models.RunnerSessionRequest("review-session", "task-1", Role.PATCH_REVIEWER, 0, 0, (), 60, "review-correlation"))
        uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("review-session", models.RunnerSessionState.SUCCEEDED, "approved"))
        uow.worker_results.record_once(models.WorkerResult("review-result", "review-session", "task-1", models.WorkerIdentity("reviewer-1", Role.PATCH_REVIEWER), TaskStatus.READY_TO_PUBLISH, "approved", 0, 0))
        uow.publications.reserve(_intent())
        uow.publications.record_prepared(_prepared(), 0)
        with pytest.raises((IdempotencyConflictError, ValidationError)):
            uow.publication_approvals.append(decision)
        verified = uow.publications.record_verification(models.VerificationResult("publication-1", "e" * 40, "sha256:" + "f" * 64, models.VerifierProfile.DEFAULT, models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED), 1)
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.request(models.PublicationEffectIntent("effect-guard", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))
        assert verified.version == 2
        uow.publication_approvals.append(decision)
        uow.publication_effects.request(models.PublicationEffectIntent("effect-guard", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.request(models.PublicationEffectIntent("effect-duplicate", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.reconcile(models.PublicationReceipt("publication-1", "effect-guard", models.PublicationEffectState.RECONCILED, models.PullRequestReceipt("publication-1", 1, "c" * 40, "0" * 40, "e" * 40)))
        receipt = models.PublicationReceipt("publication-1", "effect-guard", models.PublicationEffectState.RECONCILED, models.PullRequestReceipt("publication-1", 1, "c" * 40, "d" * 40, "e" * 40))
        assert uow.publication_effects.reconcile(receipt).receipt == receipt
        assert uow.publication_effects.reconcile(receipt).receipt == receipt
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.reconcile(models.PublicationReceipt("publication-1", "effect-guard", models.PublicationEffectState.REQUESTED))


def test_phase8_dtos_reject_path_command_and_log_like_values():
    with pytest.raises(ValidationError):
        models.VerificationResult("publication-1", "e" * 40, "sha256:" + "f" * 64, "../../shell", models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED)
    with pytest.raises(ValidationError):
        models.CheckRunObservation("publication-1", "e" * 40, "lint", "completed; curl", models.CheckRunConclusion.SUCCESS, datetime(2026, 7, 18, tzinfo=UTC))
    with pytest.raises(ValidationError):
        models.PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, "refs/meta-loop/review;curl")
    with pytest.raises(ValidationError):
        models.PublicationEffectIntent("effect-1", "publication-1", "e" * 40, "refs/meta-loop/review\nlog")
    with pytest.raises(ValidationError):
        models.CheckGateResult("publication-1", "e" * 40, ("lint; curl",), False)


def test_phase8_migration_defines_only_safe_append_only_publication_ledgers():
    migration = Path("migrations/0008_phase8_publications.sql")
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    for table in ("publications", "publication_approvals", "publication_effects", "check_run_observations"):
        assert f"CREATE TABLE {table}" in sql
    for forbidden in ("PAT", "DSN", "blob", "command", "environment", "raw_payload"):
        assert re.search(rf"\\b{forbidden}\\b", sql, re.IGNORECASE) is None
    assert "append-only" in sql
    assert sql.count("deterministic_ref = 'refs/meta-loop/' || publication_id") == 2


def test_postgres_publication_ledger_adapters_are_public():
    from meta_loop.infrastructure import postgres

    assert hasattr(postgres, "PostgresPublicationLedger")
    assert hasattr(postgres, "PostgresPublicationApprovalStore")
    assert hasattr(postgres, "PostgresPublicationEffectStore")
    assert hasattr(postgres, "PostgresCheckRunObservationStore")
