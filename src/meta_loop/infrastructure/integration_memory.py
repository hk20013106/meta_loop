from dataclasses import replace

from meta_loop.application.integration_models import CanaryResult, CanaryStatus, IntegrationIntent, IntegrationRecord, IntegrationState, MergeReceipt
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError, ValidationError
from meta_loop.infrastructure.memory import InMemoryUnitOfWork


_TERMINAL_TARGET_RELEASE_STATES = {IntegrationState.CANARY_PASSED, IntegrationState.REVERT_MERGED}


class InMemoryIntegrationLedger:
    """Single in-memory owner of Phase 9 lifecycle state."""

    def __init__(self, records: dict[str, IntegrationRecord] | None = None) -> None:
        self._records = {} if records is None else records

    def reserve(self, intent: IntegrationIntent) -> tuple[IntegrationRecord, bool]:
        current = self._records.get(intent.integration_id)
        if current is not None:
            if current.intent.canonical() != intent.canonical():
                raise IdempotencyConflictError("integration id has conflicting canonical input")
            return current, False
        for record in self._records.values():
            if (record.intent.repository_name == intent.repository_name
                    and record.intent.target_branch == intent.target_branch
                    and record.state not in _TERMINAL_TARGET_RELEASE_STATES):
                raise IdempotencyConflictError("target branch already has an unresolved integration")
        record = IntegrationRecord(intent)
        self._records[intent.integration_id] = record
        return record, True

    def get(self, integration_id: str) -> IntegrationRecord | None:
        return self._records.get(integration_id)

    def record_merge(self, receipt: MergeReceipt, expected_version: int) -> IntegrationRecord:
        current = self._records.get(receipt.integration_id)
        if current is None:
            raise ValidationError("integration does not exist")
        if current.version != expected_version or current.state is not IntegrationState.MERGE_REQUESTED:
            raise OptimisticConflictError("integration version or state does not match")
        intent = current.intent
        if (receipt.pull_request_number != intent.pull_request_number
                or receipt.base_sha != intent.base_sha
                or receipt.approved_head_sha != intent.approved_head_sha
                or receipt.merge_tree_sha != intent.prepared_tree_sha
                or receipt.target_branch != intent.target_branch):
            raise IdempotencyConflictError("merge receipt conflicts with immutable integration identity")
        updated = replace(current, state=IntegrationState.MERGED, version=current.version + 1, merge_receipt=receipt)
        self._records[intent.integration_id] = updated
        return updated

    def record_canary(self, result: CanaryResult, expected_version: int) -> IntegrationRecord:
        current = self._records.get(result.integration_id)
        if current is None or current.merge_receipt is None:
            raise ValidationError("integration has no reconciled merge")
        if current.version != expected_version or current.state not in (IntegrationState.MERGED, IntegrationState.CANARY_PENDING):
            raise OptimisticConflictError("integration version or state does not match")
        if result.sha != current.merge_receipt.merge_sha:
            raise IdempotencyConflictError("canary result is not for the exact merge SHA")
        state = {
            CanaryStatus.PENDING: IntegrationState.CANARY_PENDING,
            CanaryStatus.PASSED: IntegrationState.CANARY_PASSED,
            CanaryStatus.FAILED: IntegrationState.CANARY_FAILED,
            CanaryStatus.DRIFTED: IntegrationState.MANUAL_INTERVENTION_REQUIRED,
        }[result.status]
        updated = replace(current, state=state, version=current.version + 1, canary=result)
        self._records[result.integration_id] = updated
        return updated


class Phase9InMemoryUnitOfWork(InMemoryUnitOfWork):
    """Copy-on-write Phase 9 extension of the established in-memory UoW."""

    def __init__(self) -> None:
        super().__init__()
        self._integration_records: dict[str, IntegrationRecord] = {}
        self.integrations = InMemoryIntegrationLedger(self._integration_records)

    def __enter__(self):
        super().__enter__()
        self._working_integration_records = dict(self._integration_records)
        self.integrations = InMemoryIntegrationLedger(self._working_integration_records)
        return self

    def commit(self) -> None:
        super().commit()
        self._integration_records = self._working_integration_records
        self.integrations = InMemoryIntegrationLedger(self._integration_records)

    def __exit__(self, exc_type, exc, tb) -> None:
        super().__exit__(exc_type, exc, tb)
        self.integrations = InMemoryIntegrationLedger(self._integration_records)
