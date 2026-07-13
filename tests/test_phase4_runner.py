from datetime import UTC, datetime
from pathlib import Path

import pytest

from meta_loop.application.runners import (
    FakeRunner,
    RunnerSessionRequest,
    RunnerSessionService,
)
from meta_loop.application.models import FuseState
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator


NOW = datetime(2026, 7, 14, tzinfo=UTC)


def make_task(risk=RiskClass.R0):
    return Task.create(
        "task-4",
        TaskSource("synthetic", "fixture"),
        RepositoryTarget("public/repo", "main"),
        RiskAssessment(risk, risk, risk),
        NOW,
    )


def request(**changes):
    values = dict(
        session_id="session-4",
        task_id="task-4",
        role=Role.PLANNER,
        expected_task_version=0,
        expected_sequence=0,
        input_artifacts=(),
        timeout_seconds=60,
        correlation_id="request-4",
    )
    values.update(changes)
    return RunnerSessionRequest(**values)


def test_runner_session_is_atomic_and_idempotent():
    uow = InMemoryUnitOfWork()
    service = RunnerSessionService(FixedClock(NOW), SequentialIdGenerator("event"))
    with uow:
        uow.tasks.create(make_task())
        receipt = service.request(uow, request())
        uow.commit()

    assert receipt.created
    assert uow.runner_sessions.get("session-4").request == request()
    assert uow.events.read("task-4")[0].event_type is EventType.RUNNER_SESSION_REQUESTED

    with uow:
        repeated = service.request(uow, request())
        assert not repeated.created
        with pytest.raises(IdempotencyConflictError):
            service.request(uow, request(timeout_seconds=61))


def test_fuse_and_r3_implementer_request_are_rejected_without_session():
    uow = InMemoryUnitOfWork()
    service = RunnerSessionService(FixedClock(NOW), SequentialIdGenerator("event"))
    with uow:
        uow.tasks.create(make_task(RiskClass.R3))
        with pytest.raises(ValidationError, match="R3"):
            service.request(uow, request(role=Role.IMPLEMENTER))
        uow.fuse.set(FuseState(True, NOW))
        with pytest.raises(ValidationError, match="fuse"):
            service.request(uow, request(role=Role.DESIGN_REVIEWER))


def test_fake_runner_is_structured_and_has_no_task_mutation_capability():
    outcome = FakeRunner().run(request())
    assert outcome.session_id == "session-4"
    assert outcome.state.value == "succeeded"
    assert not hasattr(FakeRunner(), "tasks")


def test_runner_boundary_has_no_process_or_sibling_repository_dependency():
    source = Path("src/meta_loop/application/runners.py").read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "research_loop" not in source
    assert "META_LOOP_GITHUB_PAT" not in source
