from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .enums import EventType, Role
from .errors import ValidationError


@dataclass(frozen=True)
class Event:
    event_id: str
    task_id: str
    event_type: EventType
    occurred_at: datetime
    actor: Role
    payload: Mapping[str, object]
    schema_version: int
    causation_id: str | None
    correlation_id: str
    sequence: int

    def __post_init__(self) -> None:
        if not self.event_id or not self.task_id or not self.correlation_id or self.sequence < 1:
            raise ValidationError("event identifiers and sequence are required")
        if self.schema_version != 1:
            raise ValidationError("unsupported event schema version")
        if not isinstance(self.event_type, EventType) or not isinstance(self.actor, Role):
            raise ValidationError("event type and actor must be controlled values")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
