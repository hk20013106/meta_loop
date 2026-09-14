import pytest

from meta_loop.application.integration_models import CanaryResult, CanaryStatus, IntegrationIntent, IntegrationState, MergeReceipt
from meta_loop.domain.errors import IdempotencyConflictError
from meta_loop.infrastructure.memory import InMemoryUnitOfWork

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40


def make_intent(identifier="integration-1"):
    return IntegrationIntent(identifier, "publication-1", "effect-1", "task-1", "owner/repository", "main", 17, A, B, C, D, "refs/heads/meta-loop/publication-1", "gov-1", 3, 4)


def test_reserve_and_target_ownership():
    uow = InMemoryUnitOfWork()
    with uow:
        record, created = uow.integrations.reserve(make_intent())
        assert created
        assert record.state is IntegrationState.MERGE_REQUESTED
        same, created = uow.integrations.reserve(make_intent())
        assert not created and same == record
        with pytest.raises(IdempotencyConflictError):
            uow.integrations.reserve(make_intent("integration-2"))


def test_merge_and_canary_progression():
    uow = InMemoryUnitOfWork()
    with uow:
        record, _ = uow.integrations.reserve(make_intent())
        merged = uow.integrations.record_merge(MergeReceipt("integration-1", 17, A, C, E, D, A, "main"), record.version)
        assert merged.state is IntegrationState.MERGED
        pending = uow.integrations.record_canary(CanaryResult("integration-1", E, CanaryStatus.PENDING), merged.version)
        assert pending.state is IntegrationState.CANARY_PENDING
        passed = uow.integrations.record_canary(CanaryResult("integration-1", E, CanaryStatus.PASSED), pending.version)
        assert passed.state is IntegrationState.CANARY_PASSED
        second, created = uow.integrations.reserve(make_intent("integration-2"))
        assert created and second.intent.integration_id == "integration-2"
