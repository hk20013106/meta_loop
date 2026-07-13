from datetime import datetime

from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import UnsupportedSchemaVersionError
from meta_loop.domain.events import Event
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource


def event_to_dict(event: Event) -> dict[str, object]:
    return {
        "event_id": event.event_id, "task_id": event.task_id, "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(), "actor": event.actor.value, "payload": dict(event.payload),
        "schema_version": event.schema_version, "causation_id": event.causation_id,
        "correlation_id": event.correlation_id, "sequence": event.sequence,
    }


def event_from_dict(data: dict[str, object]) -> Event:
    if data.get("schema_version") != 1:
        raise UnsupportedSchemaVersionError("event schema version is unsupported")
    return Event(
        event_id=str(data["event_id"]), task_id=str(data["task_id"]), event_type=EventType(str(data["event_type"])),
        occurred_at=datetime.fromisoformat(str(data["occurred_at"])), actor=Role(str(data["actor"])),
        payload=dict(data["payload"]), schema_version=1, causation_id=data.get("causation_id") and str(data["causation_id"]),
        correlation_id=str(data["correlation_id"]), sequence=int(data["sequence"]),
    )


def task_to_dict(task: Task) -> dict[str, object]:
    return {
        "schema_version": 1, "task_id": task.task_id,
        "source": {"kind": task.source.kind, "reference": task.source.reference},
        "repository": {"name": task.repository.name, "revision": task.repository.revision},
        "risk": {"proposed": task.risk.proposed.value, "validated": task.risk.validated.value,
                 "effective": task.risk.effective.value, "escalation_reason": task.risk.escalation_reason,
                 "governance_revision": task.risk.governance_revision},
        "status": task.status.value, "version": task.version, "created_at": task.created_at.isoformat(),
        "rework_round": task.rework_round,
        "artifacts": [artifact.to_dict() for artifact in task.artifacts],
    }


def task_from_dict(data: dict[str, object]) -> Task:
    if data.get("schema_version") != 1:
        raise UnsupportedSchemaVersionError("task schema version is unsupported")
    source, repository, risk = data["source"], data["repository"], data["risk"]
    artifacts = tuple(
        ArtifactRef(ArtifactDigest(str(item["digest"]).removeprefix("sha256:")), str(item["logical_name"]), str(item["media_type"]), int(item["schema_version"]))
        for item in data["artifacts"]
    )
    return Task(
        task_id=str(data["task_id"]), source=TaskSource(str(source["kind"]), str(source["reference"])),
        repository=RepositoryTarget(str(repository["name"]), str(repository["revision"])),
        risk=RiskAssessment(RiskClass(str(risk["proposed"])), RiskClass(str(risk["validated"])), RiskClass(str(risk["effective"])),
                            risk.get("escalation_reason"), risk.get("governance_revision")),
        created_at=datetime.fromisoformat(str(data["created_at"])), status=TaskStatus(str(data["status"])),
        version=int(data["version"]), rework_round=int(data["rework_round"]), artifacts=artifacts,
    )
