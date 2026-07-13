from datetime import UTC, datetime

import pytest

from meta_loop.application.artifacts import ArtifactRegistrationService, ArtifactRequest
from meta_loop.domain.enums import RiskClass
from meta_loop.domain.errors import ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator


def test_cas_round_trip_deduplicates_and_reports_orphans(tmp_path):
    store = FilesystemArtifactStore(tmp_path / "cas", tmp_path / "repo")
    digest, size = store.put([b"hello", b" world"])
    again, again_size = store.put([b"hello world"])
    assert digest == again and size == again_size == 11
    assert store.read(digest) == b"hello world"
    assert store.scan_orphans(()) == (digest,)
    assert store.scan_orphans((digest,)) == ()


def test_cas_rejects_digest_mismatch_and_source_tree_root(tmp_path):
    with pytest.raises(ValidationError):
        FilesystemArtifactStore(tmp_path / "repo" / "cas", tmp_path / "repo")
    store = FilesystemArtifactStore(tmp_path / "cas")
    with pytest.raises(ValidationError, match="digest mismatch"):
        store.put([b"content"], "0" * 64)


def test_registration_updates_catalog_task_and_event_in_one_uow(tmp_path):
    now = datetime(2026, 7, 13, tzinfo=UTC)
    uow = InMemoryUnitOfWork()
    task = Task.create("task", TaskSource("synthetic", "x"), RepositoryTarget("public/repo", "main"), RiskAssessment(RiskClass.R0, RiskClass.R0, RiskClass.R0), now)
    with uow:
        uow.tasks.create(task)
        uow.commit()
    service = ArtifactRegistrationService(FilesystemArtifactStore(tmp_path / "cas"), SequentialIdGenerator("event"), FixedClock(now))
    with uow:
        reference = service.register(uow, task.task_id, [b"artifact"], ArtifactRequest("result.txt", "text/plain", "internal", "synthetic"), 0, 0)
        uow.commit()
    assert uow.tasks.get(task.task_id).artifacts == (reference,)
    assert uow.artifacts.get(reference.digest.value) is not None
    assert uow.events.read(task.task_id)[0].payload["artifact"] == reference.to_dict()


def test_secret_marked_artifact_is_rejected_before_blob_write(tmp_path):
    service = ArtifactRegistrationService(FilesystemArtifactStore(tmp_path / "cas"), SequentialIdGenerator("event"), FixedClock(datetime(2026, 7, 13, tzinfo=UTC)))
    with pytest.raises(ValidationError, match="secrets"):
        service.register(InMemoryUnitOfWork(), "missing", [b"secret"], ArtifactRequest("x", "text/plain", "internal", "synthetic", True), 0, 0)
