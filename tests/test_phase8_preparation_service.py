from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from meta_loop.application.models import (
    CatalogedArtifact,
    FuseState,
    PreparedHead,
    PublicationIntent,
    RunnerSessionOutcome,
    RunnerSessionRequest,
    RunnerSessionState,
    WorkerIdentity,
    WorkerResult,
)
from meta_loop.application.publication import PublicationPreparationService
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import UnsupportedGovernanceError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


NOW = datetime(2026, 9, 14, tzinfo=UTC)
PATCH = b"diff --git a/README.txt b/README.txt\n--- a/README.txt\n+++ b/README.txt\n@@ -1 +1 @@\n-before\n+after\n"
DIGEST = sha256(PATCH).hexdigest()
BASE = "c" * 40
HEAD = "e" * 40
TREE = "d" * 40


class BlobStore:
    def __init__(self, content: bytes = PATCH) -> None:
        self.content = content
        self.reads = []

    def read(self, digest: str, verify: bool = True) -> bytes:
        self.reads.append((digest, verify))
        return self.content


class Preparer:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, intent: PublicationIntent, patch: bytes) -> PreparedHead:
        self.calls.append((intent, patch))
        return PreparedHead(intent.publication_id, intent.patch_digest, intent.base_sha, TREE, HEAD, f"refs/meta-loop/{intent.publication_id}")


class Governance:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized
        self.reads = []

    def read_revision(self, revision: str):
        self.reads.append(revision)
        return {"revision": revision}

    def authorize(self, revision: str, capability: str) -> bool:
        return self.authorized and revision == "gov-8" and capability == "prepare_publication"


def publication_intent(**changes) -> PublicationIntent:
    values = {
        "publication_id": "publication-1",
        "task_id": "task-1",
        "worker_result_id": "result-1",
        "patch_digest": DIGEST,
        "repository_name": "owner/repository",
        "base_sha": BASE,
        "expected_task_version": 1,
        "expected_sequence": 0,
        "correlation_id": "correlation-1",
        "governance_revision": "gov-8",
    }
    values.update(changes)
    return PublicationIntent(**values)


def seed(uow: InMemoryUnitOfWork, risk: RiskClass = RiskClass.R1, status: TaskStatus = TaskStatus.READY_TO_PUBLISH) -> None:
    artifact = ArtifactRef(ArtifactDigest(DIGEST), "change.diff", "text/x-diff")
    initial = Task.create(
        "task-1",
        TaskSource("synthetic", "fixture"),
        RepositoryTarget("owner/repository", BASE),
        RiskAssessment(risk, risk, risk, governance_revision="gov-task"),
        NOW,
    )
    task = Task(initial.task_id, initial.source, initial.repository, initial.risk, NOW, status, 1, 0, (artifact,))
    uow.tasks.create(initial)
    uow.tasks.update(task, 0)
    uow.artifacts.register(CatalogedArtifact(artifact, "internal", "worker_result:result-1", len(PATCH)))
    request = RunnerSessionRequest("session-1", "task-1", Role.IMPLEMENTER, 0, 0, (), 60, "implementer-correlation")
    uow.runner_sessions.record_once(request)
    uow.runner_sessions.set_outcome(RunnerSessionOutcome("session-1", RunnerSessionState.SUCCEEDED, "implemented"))
    uow.worker_results.record_once(WorkerResult(
        "result-1", "session-1", "task-1", WorkerIdentity("worker-1", Role.IMPLEMENTER),
        TaskStatus.READY_TO_PUBLISH, "ready", 0, 0,
    ))


def test_preparation_service_reverifies_canonical_sources_before_recording_candidate():
    uow = InMemoryUnitOfWork()
    blobs, preparer, governance = BlobStore(), Preparer(), Governance()
    service = PublicationPreparationService(blobs, preparer, governance)
    intent = publication_intent()

    with uow:
        seed(uow)
        prepared = service.prepare(uow, intent)
        record = uow.publications.get(intent.publication_id)

        assert prepared.head_sha == HEAD
        assert record is not None and record.prepared_head == prepared
        assert record.state.value == "prepared" and record.version == 1
        assert blobs.reads == [(DIGEST, True)]
        assert preparer.calls == [(intent, PATCH)]
        assert governance.reads == ["gov-8"]


def test_preparation_service_rejects_fuse_r3_and_missing_governance_before_git():
    scenarios = ("fuse", "r3", "governance")
    for scenario in scenarios:
        uow = InMemoryUnitOfWork()
        blobs, preparer = BlobStore(), Preparer()
        governance = Governance(authorized=scenario != "governance")
        service = PublicationPreparationService(blobs, preparer, governance)
        with uow:
            seed(uow, risk=RiskClass.R3 if scenario == "r3" else RiskClass.R1)
            if scenario == "fuse":
                uow.fuse.set(FuseState(True, NOW, "gov-8"))
            error = UnsupportedGovernanceError if scenario == "governance" else ValidationError
            with pytest.raises(error):
                service.prepare(uow, publication_intent())
            assert preparer.calls == []
            assert blobs.reads == []


def test_preparation_service_rejects_task_result_and_artifact_drift_before_git():
    mutations = (
        lambda uow: uow.tasks.update(replace(uow.tasks.get("task-1"), repository=RepositoryTarget("owner/repository", "f" * 40), version=2), 1),
        lambda uow: uow.worker_results._results.__setitem__("result-1", replace(uow.worker_results.get("result-1"), worker=WorkerIdentity("reviewer", Role.PATCH_REVIEWER))),
        lambda uow: uow.artifacts._artifacts.__setitem__(DIGEST, replace(uow.artifacts.get(DIGEST), source_kind="worker_result:other")),
    )
    for mutate in mutations:
        uow = InMemoryUnitOfWork()
        blobs, preparer = BlobStore(), Preparer()
        service = PublicationPreparationService(blobs, preparer, Governance())
        with uow:
            seed(uow)
            mutate(uow)
            with pytest.raises(ValidationError):
                service.prepare(uow, publication_intent())
            assert preparer.calls == []
            assert blobs.reads == []


def test_preparation_service_rejects_corrupt_cas_bytes_before_git():
    uow = InMemoryUnitOfWork()
    blobs, preparer = BlobStore(b"corrupt"), Preparer()
    service = PublicationPreparationService(blobs, preparer, Governance())
    with uow:
        seed(uow)
        with pytest.raises(ValidationError, match="digest"):
            service.prepare(uow, publication_intent())
        assert blobs.reads == [(DIGEST, True)]
        assert preparer.calls == []
