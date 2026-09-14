from datetime import UTC, datetime

import pytest

from meta_loop.application import models
from meta_loop.application.integration import IntegrationMergeService
from meta_loop.application.integration_models import IntegrationState, MergeReceipt
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, TaskStatus
from meta_loop.domain.errors import ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.integration_memory import Phase9InMemoryUnitOfWork

NOW = datetime(2026, 9, 15, tzinfo=UTC)
BASE = "a" * 40
BASE_TREE = "b" * 40
HEAD = "c" * 40
TREE = "d" * 40
MERGE = "e" * 40
PATCH = "f" * 64
REF = "refs/heads/meta-loop/publication-1"


class TrackingUow(Phase9InMemoryUnitOfWork):
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


class Governance:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def read_revision(self, revision):
        return {"revision": revision}

    def authorize(self, revision, capability):
        return self.allowed and capability == "merge_publication"


class CheckSource:
    def __init__(self, uow):
        self.uow = uow

    def read(self, publication_id, repository_name, head_sha):
        assert not self.uow.open
        return (models.CheckRunObservation(publication_id, head_sha, "test", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS, NOW),)


class MergeGateway:
    def __init__(self, uow, timeout_once=False):
        self.uow = uow
        self.timeout_once = timeout_once
        self.remote_merged = False
        self.merge_writes = 0

    def target_branch(self, repository_name):
        assert repository_name == "owner/repository"
        return "main"

    def read_base_tree(self, repository_name, base_sha):
        assert not self.uow.open
        assert (repository_name, base_sha) == ("owner/repository", BASE)
        return BASE_TREE

    def reconcile_merge(self, intent):
        assert not self.uow.open
        if not self.remote_merged:
            self.merge_writes += 1
            self.remote_merged = True
            if self.timeout_once:
                self.timeout_once = False
                raise TimeoutError("uncertain remote response")
        return MergeReceipt(intent.integration_id, 17, BASE, HEAD, MERGE, TREE, BASE, "main")


def ready_uow() -> TrackingUow:
    uow = TrackingUow()
    task = Task(
        "task-1", TaskSource("github_issue", "1"), RepositoryTarget("owner/repository", BASE),
        RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1, governance_revision="gov-1"),
        NOW, status=TaskStatus.READY_TO_PUBLISH, version=1,
    )
    intent = models.PublicationIntent("publication-1", "task-1", "result-1", PATCH, "owner/repository", BASE, 1, 0, "correlation-1", "gov-1")
    prepared = models.PreparedHead("publication-1", PATCH, BASE, TREE, HEAD, REF)
    verification = models.VerificationResult("publication-1", HEAD, "sha256:" + "1" * 64, models.VerifierProfile.DEFAULT, models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED)
    publication = models.PublicationRecord(intent, models.PublicationState.VERIFIED, 2, prepared, verification)
    receipt = models.PublicationReceipt("publication-1", "effect-1", models.PublicationEffectState.RECONCILED, models.PullRequestReceipt("publication-1", 17, BASE, TREE, HEAD))
    effect = models.PublicationEffectRecord(models.PublicationEffectIntent("effect-1", "publication-1", HEAD, REF), receipt)
    artifact = ArtifactRef(ArtifactDigest("2" * 64), "approval.json", "application/json")
    approval = models.PublicationReviewDecision("approval-1", "publication-1", "task-1", "review-session", "review-result", artifact, PATCH, BASE, TREE, HEAD, "reviewer-1", models.ReviewDecision.APPROVED, NOW)
    uow._tasks["task-1"] = task
    uow._events["task-1"] = []
    uow._publications["publication-1"] = publication
    uow._publication_effects["effect-1"] = effect
    uow._publication_approvals["approval-1"] = approval
    return uow


def service(uow, gateway, governance=None, control_repository="owner/meta_loop"):
    return IntegrationMergeService(lambda: uow, governance or Governance(), CheckSource(uow), ("test",), gateway, control_repository)


def test_merge_persists_intent_before_remote_io_and_reconciles_exact_receipt():
    uow = ready_uow()
    gateway = MergeGateway(uow)
    result = service(uow, gateway).merge("integration-1", "publication-1", "effect-1")
    assert result.merge_sha == MERGE
    assert gateway.merge_writes == 1
    with uow:
        record = uow.integrations.get("integration-1")
    assert record.state is IntegrationState.MERGED
    assert record.intent.base_tree_sha == BASE_TREE


def test_timeout_after_remote_merge_retries_without_second_merge_write():
    uow = ready_uow()
    gateway = MergeGateway(uow, timeout_once=True)
    with pytest.raises(TimeoutError):
        service(uow, gateway).merge("integration-1", "publication-1", "effect-1")
    with uow:
        assert uow.integrations.get("integration-1").state is IntegrationState.MERGE_REQUESTED
    result = service(uow, gateway).merge("integration-1", "publication-1", "effect-1")
    assert result.merge_sha == MERGE
    assert gateway.merge_writes == 1


def test_merge_rejects_fuse_governance_self_target_and_stale_task():
    uow = ready_uow()
    with uow:
        uow.fuse.set(models.FuseState(True, NOW, "test"))
        uow.commit()
    with pytest.raises(ValidationError, match="fuse"):
        service(uow, MergeGateway(uow)).merge("integration-1", "publication-1", "effect-1")

    uow = ready_uow()
    with pytest.raises(ValidationError, match="governance"):
        service(uow, MergeGateway(uow), Governance(False)).merge("integration-1", "publication-1", "effect-1")

    uow = ready_uow()
    with pytest.raises(ValidationError, match="self"):
        service(uow, MergeGateway(uow), control_repository="owner/repository").merge("integration-1", "publication-1", "effect-1")

    uow = ready_uow()
    uow._tasks["task-1"] = Task(
        "task-1", TaskSource("github_issue", "1"), RepositoryTarget("owner/repository", BASE),
        RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1, governance_revision="gov-1"),
        NOW, status=TaskStatus.READY_TO_PUBLISH, version=2,
    )
    with pytest.raises(ValidationError, match="stale"):
        service(uow, MergeGateway(uow)).merge("integration-1", "publication-1", "effect-1")
