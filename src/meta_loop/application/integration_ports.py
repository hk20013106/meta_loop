from typing import Protocol

from meta_loop.application.integration_models import CanaryResult, IntegrationIntent, IntegrationRecord, MergeReceipt
from meta_loop.application.ports import UnitOfWork


class IntegrationLedger(Protocol):
    def reserve(self, intent: IntegrationIntent) -> tuple[IntegrationRecord, bool]: ...
    def get(self, integration_id: str) -> IntegrationRecord | None: ...
    def record_merge(self, receipt: MergeReceipt, expected_version: int) -> IntegrationRecord: ...
    def record_canary(self, result: CanaryResult, expected_version: int) -> IntegrationRecord: ...


class IntegrationUnitOfWork(UnitOfWork, Protocol):
    integrations: IntegrationLedger
