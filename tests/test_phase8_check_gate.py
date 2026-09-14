from datetime import UTC, datetime, timedelta

import pytest

from meta_loop.application import models
from meta_loop.application.publication import PublicationCheckService
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.github_checks import GitHubCheckSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
BASE = "c" * 40
TREE = "d" * 40
HEAD = "e" * 40
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


def ready_uow() -> TrackingUow:
    uow = TrackingUow()
    intent = models.PublicationIntent(
        "publication-1", "task-1", "result-1", "b" * 64,
        "owner/repository", BASE, 1, 0, "correlation-1", "governance-1",
    )
    prepared = models.PreparedHead("publication-1", "b" * 64, BASE, TREE, HEAD, REF)
    verification = models.VerificationResult(
        "publication-1", HEAD, "sha256:" + "f" * 64,
        models.VerifierProfile.DEFAULT, models.VerificationStatus.SUCCEEDED,
        models.VerificationResultCode.PASSED,
    )
    publication = models.PublicationRecord(
        intent, models.PublicationState.VERIFIED, 2, prepared, verification,
    )
    receipt = models.PublicationReceipt(
        "publication-1", "effect-1", models.PublicationEffectState.RECONCILED,
        models.PullRequestReceipt("publication-1", 17, BASE, TREE, HEAD),
    )
    effect = models.PublicationEffectRecord(
        models.PublicationEffectIntent("effect-1", "publication-1", HEAD, REF), receipt,
    )
    uow._publications["publication-1"] = publication
    uow._publication_effects["effect-1"] = effect
    return uow


class FakeCheckSource:
    def __init__(self, uow: TrackingUow, observations):
        self.uow = uow
        self.observations = tuple(observations)
        self.calls = []

    def read(self, publication_id, repository_name, head_sha):
        assert not self.uow.open
        self.calls.append((publication_id, repository_name, head_sha))
        return self.observations


def observation(name, status, conclusion, when=NOW, *, head=HEAD):
    return models.CheckRunObservation(
        "publication-1", head, name, status, conclusion, when,
    )


def test_check_service_records_exact_head_and_passes_only_current_successes():
    uow = ready_uow()
    source = FakeCheckSource(uow, (
        observation("test", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS),
        observation("lint", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS),
    ))
    result = PublicationCheckService(lambda: uow, source, ("test", "lint")).check("publication-1", "effect-1")
    assert result == models.CheckGateResult("publication-1", HEAD, ("test", "lint"), True)
    assert source.calls == [("publication-1", "owner/repository", HEAD)]
    with uow:
        stored = uow.check_observations.read("publication-1", HEAD)
    assert {item.check_name for item in stored} == {"test", "lint"}


def test_check_service_blocks_missing_pending_failure_and_stale_success():
    scenarios = (
        (),
        (observation("test", models.CheckRunStatus.IN_PROGRESS, None),),
        (observation("test", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.FAILURE),),
    )
    for current in scenarios:
        uow = ready_uow()
        with uow:
            uow.check_observations.append(observation(
                "test", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS,
                NOW - timedelta(hours=1),
            ))
            uow.commit()
        source = FakeCheckSource(uow, current)
        result = PublicationCheckService(lambda: uow, source, ("test",)).check("publication-1", "effect-1")
        assert result.passed is False


def test_check_service_rejects_wrong_head_and_fuse():
    uow = ready_uow()
    source = FakeCheckSource(uow, (
        observation("test", models.CheckRunStatus.COMPLETED, models.CheckRunConclusion.SUCCESS, head="a" * 40),
    ))
    with pytest.raises(ValidationError, match="head"):
        PublicationCheckService(lambda: uow, source, ("test",)).check("publication-1", "effect-1")

    uow = ready_uow()
    with uow:
        uow.fuse.set(models.FuseState(True, NOW, "test"))
        uow.commit()
    source = FakeCheckSource(uow, ())
    with pytest.raises(ValidationError, match="fuse"):
        PublicationCheckService(lambda: uow, source, ("test",)).check("publication-1", "effect-1")
    assert source.calls == []


class CheckTransport:
    def __init__(self, payload):
        self.payload = payload
        self.paths = []

    def get_json(self, path):
        self.paths.append(path)
        return self.payload


def test_github_check_source_reads_only_exact_commit_and_normalizes_runs():
    transport = CheckTransport({
        "total_count": 2,
        "check_runs": [
            {"name": "test", "head_sha": HEAD, "status": "completed", "conclusion": "success", "completed_at": "2026-09-14T14:59:00Z"},
            {"name": "lint", "head_sha": HEAD, "status": "in_progress", "conclusion": None, "started_at": "2026-09-14T14:58:00Z"},
        ],
    })
    values = GitHubCheckSource(transport).read("publication-1", "owner/repository", HEAD)
    assert transport.paths == [f"/repos/owner/repository/commits/{HEAD}/check-runs?per_page=100"]
    assert [(v.check_name, v.status.value, None if v.conclusion is None else v.conclusion.value) for v in values] == [
        ("test", "completed", "success"), ("lint", "in_progress", None),
    ]


def test_github_check_source_fails_closed_on_head_drift_or_bad_time():
    payloads = (
        {"total_count": 1, "check_runs": [{"name": "test", "head_sha": "a" * 40, "status": "completed", "conclusion": "success", "completed_at": "2026-09-14T14:59:00Z"}]},
        {"total_count": 1, "check_runs": [{"name": "test", "head_sha": HEAD, "status": "completed", "conclusion": "success", "completed_at": "not-a-time"}]},
    )
    for payload in payloads:
        with pytest.raises(ValidationError):
            GitHubCheckSource(CheckTransport(payload)).read("publication-1", "owner/repository", HEAD)
