from datetime import UTC, datetime

import pytest

from meta_loop.application import models
from meta_loop.application.publication import PublicationPublishingService
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


NOW = datetime(2026, 9, 14, tzinfo=UTC)
PATCH = ArtifactRef(ArtifactDigest("b" * 64), "change.diff", "text/x-diff")
REVIEW = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")
REF = "refs/heads/meta-loop/publication-1"


class TrackingUow(InMemoryUnitOfWork):
    def __init__(self):
        super().__init__()
        self.open = False

    def __enter__(self):
        value = super().__enter__()
        self.open = True
        return value

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.open = False


class FakePublisher:
    def __init__(self, uow: TrackingUow, *, fail_once: bool = False):
        self.uow = uow
        self.fail_once = fail_once
        self.calls = []

    def reconcile(self, intent, prepared, effect):
        assert not self.uow.open, "network I/O must not occur inside a UnitOfWork"
        self.calls.append((intent, prepared, effect))
        if self.fail_once:
            self.fail_once = False
            raise ValidationError("simulated uncertain GitHub failure")
        return models.PublicationReceipt(
            effect.publication_id,
            effect.effect_id,
            models.PublicationEffectState.RECONCILED,
            models.PullRequestReceipt(
                effect.publication_id,
                17,
                prepared.base_sha,
                prepared.tree_sha,
                prepared.head_sha,
            ),
        )


def seed_publication(uow: InMemoryUnitOfWork, *, approved: bool = True) -> None:
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
        "session-1", "task-1", Role.IMPLEMENTER, 1, 0, (PATCH,), 60, "impl",
    ))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("session-1", models.RunnerSessionState.SUCCEEDED, "ready"))
    uow.worker_results.record_once(models.WorkerResult(
        "result-1", "session-1", "task-1", models.WorkerIdentity("worker-1", Role.IMPLEMENTER),
        TaskStatus.READY_TO_PUBLISH, "ready", 1, 0,
    ))

    intent = models.PublicationIntent(
        "publication-1", "task-1", "result-1", PATCH.digest.value,
        "owner/repository", "c" * 40, 1, 0, "correlation-1", "governance-1",
    )
    prepared = models.PreparedHead(
        "publication-1", PATCH.digest.value, "c" * 40, "d" * 40, "e" * 40, REF,
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
        "review-session", "task-1", Role.PATCH_REVIEWER, 1, 0, (PATCH,), 60, "review",
    ))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("review-session", models.RunnerSessionState.SUCCEEDED, "approved"))
    uow.worker_results.record_once(models.WorkerResult(
        "review-result", "review-session", "task-1", models.WorkerIdentity("reviewer-1", Role.PATCH_REVIEWER),
        TaskStatus.READY_TO_PUBLISH, "approved", 1, 0,
    ))
    if approved:
        uow.publication_approvals.append(models.PublicationReviewDecision(
            "approval-1", "publication-1", "task-1", "review-session", "review-result", REVIEW,
            PATCH.digest.value, prepared.base_sha, prepared.tree_sha, prepared.head_sha,
            "reviewer-1", models.ReviewDecision.APPROVED, NOW,
        ))


def ready_uow(*, approved: bool = True) -> TrackingUow:
    uow = TrackingUow()
    with uow:
        seed_publication(uow, approved=approved)
        uow.commit()
    return uow


def test_publish_commits_durable_effect_before_network_and_reconciles_afterward():
    uow = ready_uow()
    publisher = FakePublisher(uow)
    receipt = PublicationPublishingService(lambda: uow, publisher).publish("publication-1", "effect-1")

    assert receipt.pull_request.pull_request_number == 17
    assert len(publisher.calls) == 1
    with uow:
        effect = uow.publication_effects.get("effect-1")
        assert effect is not None
        assert effect.intent.deterministic_ref == REF
        assert effect.receipt == receipt


def test_publish_recovers_after_uncertain_remote_failure_without_creating_second_effect():
    uow = ready_uow()
    publisher = FakePublisher(uow, fail_once=True)
    service = PublicationPublishingService(lambda: uow, publisher)

    with pytest.raises(ValidationError, match="uncertain"):
        service.publish("publication-1", "effect-1")

    with uow:
        durable = uow.publication_effects.get("effect-1")
        assert durable is not None and durable.receipt is None

    receipt = service.publish("publication-1", "effect-1")
    assert receipt.pull_request.pull_request_number == 17
    assert len(publisher.calls) == 2
    with uow:
        assert uow.publication_effects.get("effect-1").receipt == receipt


def test_publish_is_idempotent_after_reconciliation_and_does_not_call_network_again():
    uow = ready_uow()
    publisher = FakePublisher(uow)
    service = PublicationPublishingService(lambda: uow, publisher)
    first = service.publish("publication-1", "effect-1")
    second = service.publish("publication-1", "effect-1")
    assert first == second
    assert len(publisher.calls) == 1


def test_publish_blocks_before_effect_or_network_without_exact_head_approval_or_with_fuse():
    uow = ready_uow(approved=False)
    publisher = FakePublisher(uow)
    with pytest.raises(ValidationError, match="approval"):
        PublicationPublishingService(lambda: uow, publisher).publish("publication-1", "effect-1")
    assert publisher.calls == []

    uow = ready_uow()
    publisher = FakePublisher(uow)
    with uow:
        uow.fuse.set(models.FuseState(True, NOW, "test"))
        uow.commit()
    with pytest.raises(ValidationError, match="fuse"):
        PublicationPublishingService(lambda: uow, publisher).publish("publication-1", "effect-1")
    assert publisher.calls == []
