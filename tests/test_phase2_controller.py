from datetime import UTC, datetime

import pytest

from meta_loop.application.controller import FuseService, IntakeRequest, QueueControlService, TaskIntakeService
from meta_loop.domain.enums import RiskClass
from meta_loop.domain.errors import CreateConflictError, UnsupportedGovernanceError, ValidationError
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator


def request(**changes):
    values = dict(request_id="synthetic-1", source_kind="synthetic", source_reference="fixture", repository_name="public/repo", repository_revision="main", proposed_risk=RiskClass.R0, validated_risk=RiskClass.R0, effective_risk=RiskClass.R0, enqueue=True)
    values.update(changes)
    return IntakeRequest(**values)


def test_intake_is_atomic_and_idempotent():
    service = TaskIntakeService(FixedClock(datetime(2026, 7, 13, tzinfo=UTC)), SequentialIdGenerator("event"))
    uow = InMemoryUnitOfWork()
    with uow:
        task = service.start(uow, request())
        uow.commit()
    with uow:
        assert service.start(uow, request()) == task
        assert len(uow.events.read(task.task_id)) == 1
        with pytest.raises(CreateConflictError):
            service.start(uow, request(source_reference="changed"))


def test_engaged_fuse_rejects_new_intake():
    clock = FixedClock(datetime(2026, 7, 13, tzinfo=UTC))
    uow = InMemoryUnitOfWork()
    with uow:
        FuseService().engage(uow, clock.now())
        with pytest.raises(Exception, match="fuse is engaged"):
            TaskIntakeService(clock, SequentialIdGenerator("event")).start(uow, request())


def test_uncommitted_fuse_and_intake_are_rolled_back():
    clock = FixedClock(datetime(2026, 7, 13, tzinfo=UTC))
    uow = InMemoryUnitOfWork()
    with uow:
        TaskIntakeService(clock, SequentialIdGenerator("event")).start(uow, request())
        FuseService().engage(uow, clock.now())
    with uow:
        assert not uow.fuse.get().engaged
        assert uow.intake_ledger.get("synthetic-1") is None
        assert uow.control_events.read() == ()


def test_fuse_release_is_fail_closed_and_claim_reads_store():
    clock = FixedClock(datetime(2026, 7, 13, tzinfo=UTC))
    uow = InMemoryUnitOfWork()
    with uow:
        FuseService().engage(uow, clock.now())
        with pytest.raises(UnsupportedGovernanceError):
            FuseService().release(uow, clock.now(), None, None)
        with pytest.raises(ValidationError, match="fuse is engaged"):
            QueueControlService().claim(uow, "worker", clock.now(), __import__("datetime").timedelta(seconds=30))
