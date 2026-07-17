from datetime import UTC, datetime

import pytest

from meta_loop.application.ingestion import IssueCandidate, IssueIngestionService, IssueTaskSpec, parse_issue_task_spec
from meta_loop.application.models import FuseState, IssueIngestionRecord
from meta_loop.domain.enums import EventType, RiskClass
from meta_loop.domain.errors import IdempotencyConflictError, UnsupportedGovernanceError, ValidationError
from meta_loop.infrastructure.cas import FilesystemArtifactStore
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator
from meta_loop.infrastructure.memory import UUIDIdGenerator


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
    class Governance:
        def read_revision(self, revision):
            assert revision == "gov-7"
        def authorize(self, revision, capability):
            return revision == "gov-7" and capability == "github_issue_ingestion"
    return IssueIngestionService(
        FilesystemArtifactStore(tmp_path / "cas"),
        FixedClock(NOW),
        SequentialIdGenerator("phase7"),
        Governance(),
        "gov-7",
    )


def test_issue_contract_requires_exactly_one_schema_v1_fenced_object():
    spec = parse_issue_task_spec(body())
    assert spec == IssueTaskSpec("Update the fixture", ("tests pass",), REVISION, RiskClass.R1)

    with pytest.raises(ValidationError, match="exactly one"):
        parse_issue_task_spec(body() + body())
    with pytest.raises(ValidationError, match="unknown"):
        parse_issue_task_spec("```meta-loop-json\n{\"schema_version\":1,\"objective\":\"x\",\"acceptance_criteria\":[],\"revision\":\"%s\",\"risk\":\"R1\",\"command\":\"bad\"}\n```" % REVISION)


def test_issue_task_spec_rejects_a_non_enum_risk_directly():
    with pytest.raises(ValidationError):
        IssueTaskSpec("objective", ("criterion",), REVISION, True)


def test_issue_contract_rejects_duplicate_json_keys():
    duplicate = '{"schema_version":1,"schema_version":1,"objective":"ok","acceptance_criteria":["ok"],"revision":"%s","risk":"R1"}' % REVISION
    with pytest.raises(ValidationError, match="duplicate"):
        parse_issue_task_spec("```meta-loop-json\n" + duplicate + "\n```")


@pytest.mark.parametrize("payload", [
    {"schema_version": 1, "objective": 1, "acceptance_criteria": ["ok"], "revision": REVISION, "risk": "R1"},
    {"schema_version": 1, "objective": "ok", "acceptance_criteria": "ok", "revision": REVISION, "risk": "R1"},
    {"schema_version": 1, "objective": "ok", "acceptance_criteria": [1], "revision": REVISION, "risk": "R1"},
    {"schema_version": 1, "objective": "ok", "acceptance_criteria": ["ok"], "revision": "A" * 40, "risk": "R1"},
    {"schema_version": True, "objective": "ok", "acceptance_criteria": ["ok"], "revision": REVISION, "risk": "R1"},
])
def test_issue_contract_rejects_non_exact_field_types(payload):
    import json
    with pytest.raises(ValidationError):
        parse_issue_task_spec("```meta-loop-json\n" + json.dumps(payload) + "\n```")


def test_ingestion_atomically_creates_task_artifact_events_queue_and_ledger(tmp_path):
    uow = InMemoryUnitOfWork()
    with uow:
        receipt = service(tmp_path).ingest(uow, candidate())
        uow.commit()

    assert receipt.created
    task = uow.tasks.get(receipt.task_id)
    assert task is not None and task.source.kind == "github_issue"
    assert task.repository.name == "owner/repository"
    assert task.risk.governance_revision == "gov-7"
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


def test_trigger_event_collision_is_rejected_before_cas_write(tmp_path):
    uow = InMemoryUnitOfWork()
    intake = service(tmp_path)
    with uow:
        intake.ingest(uow, candidate())
        uow.commit()
    conflicting = IssueCandidate("github", "github:repository-1:issue-18", "owner/repository#18", "event-17", "kai", "meta-loop:ready", NOW, parse_issue_task_spec(body("other")))
    with uow, pytest.raises(IdempotencyConflictError, match="trigger"):
        intake.ingest(uow, conflicting)
    assert len([path for path in (tmp_path / "cas").rglob("*") if path.is_file()]) == 1


def test_memory_source_ledger_enforces_trigger_uniqueness_independently():
    uow = InMemoryUnitOfWork()
    first = IssueIngestionRecord("source-a", "{}", "task-a", "trigger-1")
    second = IssueIngestionRecord("source-b", "{}", "task-b", "trigger-1")
    with uow:
        uow.source_ingestions.record_once(first)
        with pytest.raises(IdempotencyConflictError, match="trigger"):
            uow.source_ingestions.record_once(second)


def test_fuse_blocks_issue_ingestion_before_any_task_is_written(tmp_path):
    uow = InMemoryUnitOfWork()
    with uow:
        uow.fuse.set(FuseState(True, NOW))
        with pytest.raises(ValidationError, match="fuse"):
            service(tmp_path).ingest(uow, candidate())
    assert uow.tasks.list() == ()


def test_governance_blocks_before_cas_or_uow_writes(tmp_path):
    class RejectedGovernance:
        def read_revision(self, revision):
            return None
        def authorize(self, revision, capability):
            return False
    store = FilesystemArtifactStore(tmp_path / "cas")
    uow = InMemoryUnitOfWork()
    intake = IssueIngestionService(store, FixedClock(NOW), SequentialIdGenerator("phase7"), RejectedGovernance(), "gov-7")
    with uow, pytest.raises(UnsupportedGovernanceError, match="authorized"):
        intake.ingest(uow, candidate())
    assert uow.tasks.list() == ()
    assert list((tmp_path / "cas").rglob("*")) == []


def test_cas_blob_can_remain_when_uow_rolls_back(tmp_path):
    uow = InMemoryUnitOfWork()
    with uow:
        service(tmp_path).ingest(uow, candidate())
    assert uow.tasks.list() == ()
    assert any(path.is_file() for path in (tmp_path / "cas").rglob("*"))


def test_runtime_uuid_generator_does_not_restart_a_process_local_sequence():
    generator = UUIDIdGenerator()
    assert generator.new() != generator.new()
