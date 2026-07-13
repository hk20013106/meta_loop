from datetime import UTC, datetime

import pytest

from meta_loop.domain.enums import EventType, Role
from meta_loop.domain.errors import UnsupportedSchemaVersionError
from meta_loop.domain.events import Event
from meta_loop.serialization.codecs import event_from_dict, event_to_dict


def test_event_serialization_round_trip():
    original = Event(
        event_id="event-1", task_id="task-1", event_type=EventType.TASK_RECEIVED,
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC), actor=Role.SYSTEM,
        payload={"source": "manual"}, schema_version=1, causation_id=None,
        correlation_id="correlation-1", sequence=1,
    )

    assert event_from_dict(event_to_dict(original)) == original


def test_unknown_schema_version_fails_closed():
    data = event_to_dict(Event(
        event_id="event-1", task_id="task-1", event_type=EventType.TASK_RECEIVED,
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC), actor=Role.SYSTEM,
        payload={}, schema_version=1, causation_id=None, correlation_id="c", sequence=1,
    ))
    data["schema_version"] = 2

    with pytest.raises(UnsupportedSchemaVersionError):
        event_from_dict(data)
