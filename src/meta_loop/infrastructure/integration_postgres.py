import json

from meta_loop.application.integration_models import (
    CanaryResult,
    CanaryStatus,
    IntegrationIntent,
    IntegrationRecord,
    IntegrationState,
    MergeReceipt,
    RevertCandidate,
)
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError, ValidationError
from meta_loop.infrastructure.postgres import PostgresUnitOfWork


_TERMINAL_TARGET_RELEASE_STATES = {IntegrationState.CANARY_PASSED, IntegrationState.REVERT_MERGED}


def _json(value) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _intent(data) -> IntegrationIntent:
    value = _json(data)
    return IntegrationIntent(
        str(value["integration_id"]),
        str(value["publication_id"]),
        str(value["effect_id"]),
        str(value["task_id"]),
        str(value["repository_name"]),
        str(value["target_branch"]),
        int(value["pull_request_number"]),
        str(value["base_sha"]),
        str(value["base_tree_sha"]),
        str(value["approved_head_sha"]),
        str(value["prepared_tree_sha"]),
        str(value["deterministic_ref"]),
        str(value["governance_revision"]),
        int(value["expected_task_version"]),
        int(value["expected_sequence"]),
        int(value["schema_version"]),
    )


def _merge(data) -> MergeReceipt | None:
    if data is None:
        return None
    value = _json(data)
    return MergeReceipt(
        str(value["integration_id"]), int(value["pull_request_number"]),
        str(value["base_sha"]), str(value["approved_head_sha"]),
        str(value["merge_sha"]), str(value["merge_tree_sha"]),
        str(value["parent_sha"]), str(value["target_branch"]), int(value["schema_version"]),
    )


def _canary(data) -> CanaryResult | None:
    if data is None:
        return None
    value = _json(data)
    return CanaryResult(str(value["integration_id"]), str(value["sha"]), CanaryStatus(str(value["status"])), int(value["schema_version"]))


def _revert(data) -> RevertCandidate | None:
    if data is None:
        return None
    value = _json(data)
    return RevertCandidate(str(value["integration_id"]), str(value["parent_sha"]), str(value["tree_sha"]), str(value["head_sha"]), str(value["deterministic_ref"]), int(value["schema_version"]))


class PostgresIntegrationLedger:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    @staticmethod
    def _record(row) -> IntegrationRecord:
        return IntegrationRecord(
            _intent(row[0]), IntegrationState(str(row[1])), int(row[2]),
            _merge(row[3]), _canary(row[4]), _revert(row[5]),
        )

    def get(self, integration_id: str) -> IntegrationRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_input, state, version, merge_receipt_json, canary_json, revert_candidate_json FROM integrations WHERE integration_id = %s",
                (integration_id,),
            )
            row = cursor.fetchone()
        return None if row is None else self._record(row)

    def reserve(self, intent: IntegrationIntent) -> tuple[IntegrationRecord, bool]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_input, state, version, merge_receipt_json, canary_json, revert_candidate_json FROM integrations WHERE integration_id = %s FOR UPDATE",
                (intent.integration_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                current = self._record(row)
                if current.intent.canonical() != intent.canonical():
                    raise IdempotencyConflictError("integration id has conflicting canonical input")
                return current, False
            cursor.execute(
                """INSERT INTO integrations (
                    integration_id, publication_id, effect_id, task_id, repository_name, target_branch,
                    pull_request_number, base_sha, base_tree_sha, approved_head_sha, prepared_tree_sha,
                    deterministic_ref, governance_revision, expected_task_version, expected_sequence,
                    canonical_input, state, version, schema_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'merge_requested',0,1)
                ON CONFLICT DO NOTHING""",
                (
                    intent.integration_id, intent.publication_id, intent.effect_id, intent.task_id,
                    intent.repository_name, intent.target_branch, intent.pull_request_number,
                    intent.base_sha, intent.base_tree_sha, intent.approved_head_sha, intent.prepared_tree_sha,
                    intent.deterministic_ref, intent.governance_revision, intent.expected_task_version,
                    intent.expected_sequence, intent.canonical(),
                ),
            )
            if cursor.rowcount == 1:
                return IntegrationRecord(intent), True
            cursor.execute(
                "SELECT integration_id, canonical_input, state, version, merge_receipt_json, canary_json, revert_candidate_json FROM integrations WHERE integration_id = %s OR (repository_name = %s AND target_branch = %s AND state NOT IN ('canary_passed','revert_merged')) FOR UPDATE",
                (intent.integration_id, intent.repository_name, intent.target_branch),
            )
            conflict = cursor.fetchone()
            if conflict is None:
                raise IdempotencyConflictError("integration reservation conflicted concurrently")
            if str(conflict[0]) == intent.integration_id:
                current = self._record(conflict[1:])
                if current.intent.canonical() == intent.canonical():
                    return current, False
            raise IdempotencyConflictError("target branch already has an unresolved integration")

    def record_merge(self, receipt: MergeReceipt, expected_version: int) -> IntegrationRecord:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_input, state, version, merge_receipt_json, canary_json, revert_candidate_json FROM integrations WHERE integration_id = %s FOR UPDATE",
                (receipt.integration_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("integration does not exist")
            current = self._record(row)
            if current.version != expected_version or current.state is not IntegrationState.MERGE_REQUESTED:
                raise OptimisticConflictError("integration version or state does not match")
            intent = current.intent
            if (receipt.pull_request_number != intent.pull_request_number
                    or receipt.base_sha != intent.base_sha
                    or receipt.approved_head_sha != intent.approved_head_sha
                    or receipt.merge_tree_sha != intent.prepared_tree_sha
                    or receipt.target_branch != intent.target_branch):
                raise IdempotencyConflictError("merge receipt conflicts with immutable integration identity")
            cursor.execute(
                "UPDATE integrations SET state = 'merged', version = version + 1, merge_receipt_json = %s::jsonb, updated_at = now() WHERE integration_id = %s AND version = %s",
                (receipt.canonical(), receipt.integration_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise OptimisticConflictError("integration version does not match")
        return IntegrationRecord(intent, IntegrationState.MERGED, expected_version + 1, receipt)

    def record_canary(self, result: CanaryResult, expected_version: int) -> IntegrationRecord:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_input, state, version, merge_receipt_json, canary_json, revert_candidate_json FROM integrations WHERE integration_id = %s FOR UPDATE",
                (result.integration_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("integration does not exist")
            current = self._record(row)
            if current.merge_receipt is None:
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
            cursor.execute(
                "UPDATE integrations SET state = %s, version = version + 1, canary_json = %s::jsonb, updated_at = now() WHERE integration_id = %s AND version = %s",
                (state.value, result.canonical(), result.integration_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise OptimisticConflictError("integration version does not match")
        return IntegrationRecord(current.intent, state, expected_version + 1, current.merge_receipt, result, current.revert_candidate)


class Phase9PostgresUnitOfWork(PostgresUnitOfWork):
    """PostgreSQL UoW extension; existing Phase 1-8 stores remain owned by the base UoW."""

    def __enter__(self):
        super().__enter__()
        self.integrations = PostgresIntegrationLedger(self._connection)
        return self
