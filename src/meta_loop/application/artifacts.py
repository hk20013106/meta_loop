"""Atomic catalog/task/event registration after a CAS blob is durable."""

from dataclasses import dataclass

from meta_loop.application.models import CatalogedArtifact
from meta_loop.application.ports import IdGenerator, UnitOfWork
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef, validate_logical_name
from meta_loop.domain.enums import EventType, Role
from meta_loop.domain.errors import TaskNotFoundError, ValidationError
from meta_loop.domain.events import Event


@dataclass(frozen=True)
class ArtifactRequest:
    logical_name: str
    media_type: str
    classification: str
    source_kind: str
    contains_secrets: bool = False

    def __post_init__(self) -> None:
        validate_logical_name(self.logical_name)


class ArtifactRegistrationService:
    def __init__(self, store, ids: IdGenerator, clock) -> None:
        self._store, self._ids, self._clock = store, ids, clock

    def register(self, uow: UnitOfWork, task_id: str, chunks, request: ArtifactRequest, expected_version: int, expected_sequence: int) -> ArtifactRef:
        if request.contains_secrets:
            raise ValidationError("artifacts containing secrets are rejected")
        digest, size = self._store.put(chunks)
        reference = ArtifactRef(ArtifactDigest(digest), request.logical_name, request.media_type)
        task = uow.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("task does not exist")
        uow.artifacts.register(CatalogedArtifact(reference, request.classification, request.source_kind, size))
        updated = task.attach_artifact(reference)
        if updated != task:
            uow.tasks.update(updated, expected_version)
        event = Event(self._ids.new(), task_id, EventType.ARTIFACT_REGISTERED, self._clock.now(), Role.SYSTEM, {"artifact": reference.to_dict()}, 1, None, task_id, expected_sequence + 1)
        uow.events.append(event, expected_sequence)
        return reference
