from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from meta_loop.application.models import WorkerIdentity, WorkerResult
from meta_loop.application.artifacts import ArtifactRegistrationService, ArtifactRequest
from meta_loop.application.runners import FakeRunner, RunnerSessionRequest, RunnerSessionService
from meta_loop.application.workers import ResultPublicationService, WorkerOrchestrationService
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator
from meta_loop.infrastructure.cas import FilesystemArtifactStore


NOW = datetime(2026, 7, 14, tzinfo=UTC)


def setup_session(risk=RiskClass.R0):
    uow = InMemoryUnitOfWork()
    task = replace(Task.create("task-5", TaskSource("synthetic", "fixture"), RepositoryTarget("public/repo", "main"), RiskAssessment(risk, risk, risk), NOW), status=TaskStatus.PLANNED)
    request = RunnerSessionRequest("session-5", task.task_id, Role.DESIGN_REVIEWER, 0, 0, (), 60, "phase5")
    with uow:
        uow.tasks.create(task)
        RunnerSessionService(FixedClock(NOW), SequentialIdGenerator("event")).request(uow, request)
        uow.queue.enqueue(task.task_id, 0, NOW, "queue-5")
        uow.queue.claim("worker-5", NOW, timedelta(seconds=60))
        uow.commit()
    return uow, task, request


def test_worker_outcome_is_idempotent_and_does_not_mutate_task():
    uow, task, request = setup_session()
    service = WorkerOrchestrationService()
    with uow:
        outcome = service.run(uow, FakeRunner(), request.session_id)
        assert outcome.state.value == "succeeded"
        assert uow.tasks.get(task.task_id) == task
        assert service.run(uow, FakeRunner(), request.session_id) == outcome
        uow.commit()


def test_r3_and_fuse_block_worker_execution():
    uow, _, request = setup_session(RiskClass.R3)
    with uow:
        uow.fuse.set(__import__("meta_loop.application.models", fromlist=["FuseState"]).FuseState(True, NOW))
        with pytest.raises(ValidationError, match="fuse"):
            WorkerOrchestrationService().run(uow, FakeRunner(), request.session_id)


def test_result_ledger_rejects_changed_duplicate_payload():
    result = WorkerResult("result-5", "session-5", "task-5", WorkerIdentity("worker-5", Role.DESIGN_REVIEWER), TaskStatus.DESIGN_REVIEW, "safe summary", 0, 1)
    uow, _, _ = setup_session()
    with uow:
        assert uow.worker_results.record_once(result).created
        assert not uow.worker_results.record_once(result).created
        with pytest.raises(IdempotencyConflictError):
            uow.worker_results.record_once(replace(result, summary="changed"))


def test_result_publication_registers_only_artifact_reference_and_completes_lease(tmp_path):
    uow, task, request = setup_session()
    result = WorkerResult("publish-5", request.session_id, task.task_id, WorkerIdentity("worker-5", Role.DESIGN_REVIEWER), TaskStatus.DESIGN_REVIEW, "review evidence", 0, 1)
    registration = ArtifactRegistrationService(FilesystemArtifactStore(tmp_path / "cas"), SequentialIdGenerator("artifact-event"), FixedClock(NOW))
    with uow:
        WorkerOrchestrationService().run(uow, FakeRunner(), request.session_id)
        receipt = ResultPublicationService(registration, SequentialIdGenerator("result-event"), FixedClock(NOW)).publish(uow, result, [b"safe output"], ArtifactRequest("review.txt", "text/plain", "internal", "fake"), NOW)
        uow.commit()
    assert receipt.created
    assert uow.tasks.get(task.task_id).status is TaskStatus.DESIGN_REVIEW
    payloads = [event.payload for event in uow.events.read(task.task_id)]
    assert all(b"safe output" not in repr(payload).encode() for payload in payloads)
