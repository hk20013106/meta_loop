from meta_loop.application.integration_models import IntegrationIntent, IntegrationState, MergeReceipt
from meta_loop.application.models import ReviewDecision
from meta_loop.application.publication import PublicationCheckService
from meta_loop.domain.enums import RiskClass, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError


class IntegrationMergeService:
    """Persist exact merge intent before remote I/O and reconcile after the transaction closes."""

    def __init__(
        self,
        uow_factory,
        governance,
        check_source,
        required_checks: tuple[str, ...],
        gateway,
        control_repository_name: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._governance = governance
        self._check_service = PublicationCheckService(uow_factory, check_source, required_checks)
        self._gateway = gateway
        self._control_repository_name = control_repository_name

    def merge(self, integration_id: str, publication_id: str, effect_id: str) -> MergeReceipt:
        check_result = self._check_service.check(publication_id, effect_id)
        if not check_result.passed:
            raise ValidationError("fresh publication checks have not passed")

        with self._uow_factory() as uow:
            publication, effect, task = self._validated_phase8(uow, publication_id, effect_id)
            existing = uow.integrations.get(integration_id)
            if existing is not None and existing.state is IntegrationState.MERGED:
                if existing.merge_receipt is None:
                    raise IdempotencyConflictError("merged integration has no receipt")
                return existing.merge_receipt
            immutable = (
                publication.intent,
                publication.prepared_head,
                effect.receipt.pull_request,
                task.version,
                len(uow.events.read(task.task_id)),
            )

        publication_intent, prepared, pull_request, task_version, event_sequence = immutable
        base_tree_sha = self._gateway.read_base_tree(publication_intent.repository_name, publication_intent.base_sha)
        intent = IntegrationIntent(
            integration_id,
            publication_id,
            effect_id,
            publication_intent.task_id,
            publication_intent.repository_name,
            self._target_branch(publication_intent.repository_name, pull_request),
            pull_request.pull_request_number,
            publication_intent.base_sha,
            base_tree_sha,
            prepared.head_sha,
            prepared.tree_sha,
            prepared.deterministic_ref,
            publication_intent.governance_revision,
            task_version,
            event_sequence,
        )

        with self._uow_factory() as uow:
            publication_now, effect_now, task_now = self._validated_phase8(uow, publication_id, effect_id)
            if (publication_now.intent.canonical() != publication_intent.canonical()
                    or publication_now.prepared_head != prepared
                    or effect_now.receipt.pull_request != pull_request
                    or task_now.version != task_version
                    or len(uow.events.read(task_now.task_id)) != event_sequence):
                raise ValidationError("integration source state became stale")
            record, _ = uow.integrations.reserve(intent)
            if record.state is IntegrationState.MERGED:
                if record.merge_receipt is None:
                    raise IdempotencyConflictError("merged integration has no receipt")
                return record.merge_receipt
            if record.state is not IntegrationState.MERGE_REQUESTED:
                raise ValidationError("integration is not eligible for merge reconciliation")
            durable_intent = record.intent
            uow.commit()

        receipt = self._gateway.reconcile_merge(durable_intent)
        self._validate_receipt(durable_intent, receipt)

        with self._uow_factory() as uow:
            current = uow.integrations.get(integration_id)
            if current is None or current.intent.canonical() != durable_intent.canonical():
                raise IdempotencyConflictError("durable integration intent drifted before reconciliation")
            if current.state is IntegrationState.MERGED:
                if current.merge_receipt is None or current.merge_receipt.canonical() != receipt.canonical():
                    raise IdempotencyConflictError("integration has a conflicting merge receipt")
                return current.merge_receipt
            updated = uow.integrations.record_merge(receipt, current.version)
            uow.commit()
            if updated.merge_receipt is None:
                raise ValidationError("merge reconciliation did not persist a receipt")
            return updated.merge_receipt

    def _validated_phase8(self, uow, publication_id: str, effect_id: str):
        publication, effect = PublicationCheckService._validated_state(uow, publication_id, effect_id)
        task = uow.tasks.get(publication.intent.task_id)
        if task is None:
            raise ValidationError("integration task does not exist")
        if task.status is not TaskStatus.READY_TO_PUBLISH or task.risk.effective is RiskClass.R3:
            raise ValidationError("integration task is not merge eligible")
        if (task.version != publication.intent.expected_task_version
                or len(uow.events.read(task.task_id)) != publication.intent.expected_sequence):
            raise ValidationError("integration task state is stale")
        if (task.repository.name != publication.intent.repository_name
                or task.repository.revision != publication.intent.base_sha):
            raise ValidationError("integration task repository is stale")
        if task.repository.name.lower() == self._control_repository_name.lower():
            raise ValidationError("Meta Loop self integration is not allowed")
        approvals = uow.publication_approvals.read(publication_id, publication.prepared_head.head_sha)
        if not any(
            approval.decision is ReviewDecision.APPROVED
            and approval.task_id == task.task_id
            and approval.patch_digest == publication.intent.patch_digest
            and approval.base_sha == publication.prepared_head.base_sha
            and approval.tree_sha == publication.prepared_head.tree_sha
            and approval.head_sha == publication.prepared_head.head_sha
            for approval in approvals
        ):
            raise ValidationError("integration requires exact-head publication approval")
        self._authorize(publication.intent.governance_revision)
        return publication, effect, task

    def _authorize(self, revision: str) -> None:
        try:
            self._governance.read_revision(revision)
            if not self._governance.authorize(revision, "merge_publication"):
                raise ValidationError("merge governance is not authorized")
        except ValidationError:
            raise
        except Exception as error:
            raise ValidationError("merge governance is unavailable") from error

    @staticmethod
    def _target_branch(repository_name: str, pull_request) -> str:
        # Phase 8 stores the base SHA, not branch name; Phase 9's gateway/config owns the target branch.
        # The gateway exposes the configured branch without performing a write.
        branch = getattr(pull_request, "target_branch", None)
        if isinstance(branch, str) and branch:
            return branch
        return "main"

    @staticmethod
    def _validate_receipt(intent: IntegrationIntent, receipt: MergeReceipt) -> None:
        if (not isinstance(receipt, MergeReceipt)
                or receipt.integration_id != intent.integration_id
                or receipt.pull_request_number != intent.pull_request_number
                or receipt.base_sha != intent.base_sha
                or receipt.approved_head_sha != intent.approved_head_sha
                or receipt.merge_tree_sha != intent.prepared_tree_sha
                or receipt.parent_sha != intent.base_sha
                or receipt.target_branch != intent.target_branch):
            raise IdempotencyConflictError("merge receipt does not match immutable integration identity")
