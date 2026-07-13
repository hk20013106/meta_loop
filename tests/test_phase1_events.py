from datetime import UTC, datetime

import pytest

from meta_loop.domain.enums import EventType, Role
from meta_loop.domain.errors import SequenceConflictError, ValidationError
from meta_loop.domain.events import Event
from meta_loop.infrastructure.memory import InMemoryEventStore


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def event(sequence: int, event_id: str = "event-1") -> Event:
    return Event(
        event_id=event_id,
        task_id="task-1",
        event_type=EventType.TASK_RECEIVED,
        occurred_at=NOW,
        actor=Role.SYSTEM,
        payload={"source": "manual"},
        schema_version=1,
        causation_id=None,
        correlation_id="correlation-1",
        sequence=sequence,
    )


def test_event_store_appends_once_and_returns_immutable_history():
    store = InMemoryEventStore()
    store.append(event(1), expected_sequence=0)

    history = store.read("task-1")

    assert history == (event(1),)
    with pytest.raises(AttributeError):
        history.append(event(2))


def test_event_sequence_conflict_is_rejected():
    store = InMemoryEventStore()
    store.append(event(1), expected_sequence=0)

    with pytest.raises(SequenceConflictError):
        store.append(event(2, "event-2"), expected_sequence=0)


def test_exact_duplicate_event_is_idempotent_but_changed_duplicate_is_rejected():
    store = InMemoryEventStore()
    first = event(1)

    assert store.append(first, expected_sequence=0) == first
    assert store.append(first, expected_sequence=1) == first
    with pytest.raises(ValidationError):
        store.append(event(2), expected_sequence=1)
