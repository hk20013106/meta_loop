from meta_loop.application.ports import ArtifactStore, Clock, EventStore, GovernanceReader, IdGenerator, TaskQueue, TaskRepository
from meta_loop.infrastructure.postgres import PostgresEventStore, PostgresTaskQueue, PostgresTaskRepository


def test_phase1_ports_and_postgres_adapters_are_explicit_boundaries():
    assert all(hasattr(port, name) for port, name in (
        (TaskRepository, "save"), (TaskQueue, "claim"), (TaskQueue, "heartbeat"),
        (TaskQueue, "complete"), (TaskQueue, "fail"), (TaskQueue, "recover_expired"), (EventStore, "append"),
        (ArtifactStore, "get"), (Clock, "now"), (IdGenerator, "new"), (GovernanceReader, "authorize"),
    ))
    assert all(adapter.__module__ == "meta_loop.infrastructure.postgres" for adapter in (
        PostgresTaskRepository, PostgresEventStore, PostgresTaskQueue,
    ))
