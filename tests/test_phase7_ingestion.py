from datetime import UTC, datetime

import pytest

from meta_loop.application.ingestion import IssueCandidate, IssueIngestionService, IssueTaskSpec, parse_issue_task_spec
from meta_loop.application.models import FuseState
from meta_loop.domain.enums import EventType, RiskClass
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator


NOW = datetime(2026, 7, 16, tzinfo=UTC)
REVISION = "a" * 40


def body(objective: str = "Update the fixture") -> str:
    return """Issue context ignored by the contract parser.

```meta-loop-json
{"schema_version":1,"objective":"%s","acceptance_criteria":["tests pass"],"revision":"%s","risk":"R1"}
```
""" % (objective, REVISION)


def candidate(objective: str = "Update the fixture") -> IssueCandidate:
    return IssueCandidate(
        provider="github",
        source_key="github:repository-1:issue-17",
        source_reference="owner/repository#17",
        trigger_event_id="event-17",
        trigger_actor="kai",
        trigger_label="meta-loop:ready",
        occurred_at=NOW,
        spec=parse_issue_task_spec(body(objective)),
    )


def service(tmp_path):
    return IssueIngestionService(
        FilesystemArtifactStore(tmp_path / "cas"),
        FixedClock(NOW),
        SequentialIdGenerator("phase7"),
    )


def test_issue_contract_requires_exactly_one_schema_v1_fenced_object():
    spec = parse_issue_task_spec(body())
    assert spec == IssueTaskSpec("Update the fixture", ("tests pass",), REVISION, RiskClass.R1)

    with pytest.raises(ValidationError, match="exactly one"):
        parse_issue_task_spec(body() + body())
    with pytest.raises(ValidationError, match="unknown"):
        parse_issue_task_spec("```meta-loop-json\n{\"schema_version\":1,\"objective\":\"x\",\"acceptance_criteria\":[],\"revision\":\"%s\",\"risk\":\"R1\",\"command\":\"bad\"}\n```" % REVISION)


def test_ingestion_atomically_creates_task_artifact_events_queue_and_ledger(tmp_path):
    uow = InMemoryUnitOfWork()
    with uow:
        receipt = service(tmp_path).ingest(uow, candidate())
        uow.commit()

    assert receipt.created
    task = uow.tasks.get(receipt.task_id)
    assert task is not None and task.source.kind == "github_issue"
    assert task.repository.name == "owner/repository"
    assert len(task.artifacts) == 1
    assert [event.event_type for event in uow.events.read(task.task_id)] == [
        EventType.TASK_RECEIVED,
        EventType.ARTIFACT_REGISTERED,
        EventType.SOURCE_INGESTED,
    ]


def test_issue_ingestion_is_idempotent_but_rejects_source_drift(tmp_path):
    uow = InMemoryUnitOfWork()
    intake = service(tmp_path)
    with uow:
        first = intake.ingest(uow, candidate())
        uow.commit()
    with uow:
        assert not intake.ingest(uow, candidate()).created
        with pytest.raises(IdempotencyConflictError, match="source"):
            intake.ingest(uow, candidate("Changed objective"))


def test_fuse_blocks_issue_ingestion_before_any_task_is_written(tmp_path):
    uow = InMemoryUnitOfWork()
    with uow:
        uow.fuse.set(FuseState(True, NOW))
        with pytest.raises(ValidationError, match="fuse"):
            service(tmp_path).ingest(uow, candidate())
    assert uow.tasks.list() == ()
