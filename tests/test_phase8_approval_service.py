from datetime import UTC, datetime

import pytest

from meta_loop.application import models
from meta_loop.application.publication import PublicationApprovalService
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


NOW = datetime(2026, 9, 14, tzinfo=UTC)
PATCH = ArtifactRef(ArtifactDigest("b" * 64), "change.diff", "text/x-diff")
REVIEW = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")


def seed_verified(uow: InMemoryUnitOfWork, *, reviewer_role=Role.PATCH_REVIEWER, reviewer_state=models.RunnerSessionState.SUCCEEDED):
    initial = Task.create(
        "task-1",
        TaskSource("synthetic", "fixture"),
        RepositoryTarget("owner/repository", "c" * 40),
        RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1, governance_revision="governance-1"),
        NOW,
    )
    task = Task(
        "task-1", initial.source, initial.repository, initial.risk, NOW,
        TaskStatus.READY_TO_PUBLISH, 1, 0, (PATCH,),
    )
    uow.tasks.create(initial)
    uow.tasks.update(task, 0)
    uow.artifacts.register(models.CatalogedArtifact(PATCH, "internal", "worker_result:result-1", 10))
    uow.runner_sessions.record_once(models.RunnerSessionRequest(
        "session-1", "task-1", Role.IMPLEMENTER, 1, 0, (PATCH,), 60, "implementer-correlation",
    ))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("session-1", models.RunnerSessionState.SUCCEEDED, "ready"))
    uow.worker_results.record_once(models.WorkerResult(
        "result-1", "session-1", "task-1", models.WorkerIdentity("worker-1", Role.IMPLEMENTER),
        TaskStatus.READY_TO_PUBLISH, "ready", 1, 0,
    ))
    intent = models.PublicationIntent(
        "publication-1", "task-1", "result-1", PATCH.digest.value, "owner/repository", "c" * 40,
        1, 0, "correlation-1", "governance-1",
    )
    prepared = models.PreparedHead(
        "publication-1", PATCH.digest.value, "c" * 40, "d" * 40, "e" * 40,
        "refs/meta-loop/publication-1",
    )
    uow.publications.reserve(intent)
    uow.publications.record_prepared(prepared, 0)
    uow.publications.record_verification(models.VerificationResult(
        "publication-1", prepared.head_sha, "sha256:" + "f" * 64,
        models.VerifierProfile.DEFAULT, models.VerificationStatus.SUCCEEDED,
        models.VerificationResultCode.PASSED,
    ), 1)

    uow.artifacts.register(models.CatalogedArtifact(REVIEW, "internal", "worker_result:review-result", 10))
    uow.runner_sessions.record_once(models.RunnerSessionRequest(
        "review-session", "task-1", reviewer_role, 1, 0, (PATCH,), 60, "review-correlation",
    ))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("review-session", reviewer_state, "reviewed"))
    uow.worker_results.record_once(models.WorkerResult(
        "review-result", "review-session", "task-1", models.WorkerIdentity("reviewer-1", reviewer_role),
        TaskStatus.READY_TO_PUBLISH, "approved", 1, 0,
    ))


def test_approval_service_derives_exact_head_and_reviewer_from_stored_facts():
    uow = InMemoryUnitOfWork()
    with uow:
        seed_verified(uow)
        decision, created = PublicationApprovalService().approve(
            uow, "publication-1", "approval-1", "review-result", REVIEW.digest.value, NOW,
        )
        assert created
        assert decision.decision is models.ReviewDecision.APPROVED
        assert decision.task_id == "task-1"
        assert decision.session_id == "review-session"
        assert decision.reviewer_id == "reviewer-1"
        assert decision.approval_artifact == REVIEW
        assert decision.patch_digest == PATCH.digest.value
        assert decision.base_sha == "c" * 40
        assert decision.tree_sha == "d" * 40
        assert decision.head_sha == "e" * 40
        assert PublicationApprovalService().approve(
            uow, "publication-1", "approval-1", "review-result", REVIEW.digest.value, NOW,
        ) == (decision, False)


def test_approval_service_rejects_noncanonical_review_artifact_and_reviewer_input():
    uow = InMemoryUnitOfWork()
    with uow:
        seed_verified(uow)
        unrelated = ArtifactRef(ArtifactDigest("9" * 64), "other.json", "application/json")
        uow.artifacts.register(models.CatalogedArtifact(unrelated, "internal", "review", 1))
        with pytest.raises(ValidationError, match="artifact"):
            PublicationApprovalService().approve(
                uow, "publication-1", "approval-1", "review-result", unrelated.digest.value, NOW,
            )


def test_approval_service_fails_closed_when_fuse_is_engaged_or_review_session_failed():
    uow = InMemoryUnitOfWork()
    with uow:
        seed_verified(uow)
        uow.fuse.set(models.FuseState(True, NOW, "test"))
        with pytest.raises(ValidationError, match="fuse"):
            PublicationApprovalService().approve(
                uow, "publication-1", "approval-1", "review-result", REVIEW.digest.value, NOW,
            )

    uow = InMemoryUnitOfWork()
    with uow:
        seed_verified(uow, reviewer_state=models.RunnerSessionState.FAILED)
        with pytest.raises(ValidationError, match="reviewer"):
            PublicationApprovalService().approve(
                uow, "publication-1", "approval-1", "review-result", REVIEW.digest.value, NOW,
            )
