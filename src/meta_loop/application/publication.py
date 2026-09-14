"""Application gates for deterministic Phase 8 publication."""

from hashlib import sha256

from meta_loop.application.checks import RequiredCheckEvaluator, RequiredCheckStatus
from meta_loop.application.models import (
    CheckGateResult,
    CheckRunObservation,
    PreparedHead,
    PublicationEffectIntent,
    PublicationEffectState,
    PublicationIntent,
    PublicationReceipt,
    PublicationReviewDecision,
    PublicationState,
    ReviewDecision,
    RunnerSessionState,
    VerificationResultCode,
    VerificationStatus,
)
from meta_loop.application.ports import CandidatePreparer, ContentAddressedBlobStore, GovernanceReader, UnitOfWork
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError


class PublicationPreparationService:
    def __init__(self, blobs: ContentAddressedBlobStore, preparer: CandidatePreparer, governance: GovernanceReader) -> None:
        self._blobs = blobs
        self._preparer = preparer
        self._governance = governance

    def prepare(self, uow: UnitOfWork, intent: PublicationIntent) -> PreparedHead:
        self._validate_preconditions(uow, intent)
        artifact = uow.artifacts.get(intent.patch_digest)
        result = uow.worker_results.get(intent.worker_result_id)
        if artifact is None or result is None:
            raise ValidationError("publication references are incomplete")

        patch = self._blobs.read(intent.patch_digest, verify=True)
        if sha256(patch).hexdigest() != intent.patch_digest:
            raise ValidationError("publication CAS digest mismatch")

        prepared = self._preparer.prepare(intent, patch)
        if (prepared.publication_id != intent.publication_id
                or prepared.patch_digest != intent.patch_digest
                or prepared.base_sha != intent.base_sha
                or prepared.deterministic_ref != f"refs/heads/meta-loop/{intent.publication_id}"):
            raise IdempotencyConflictError("prepared candidate conflicts with publication intent")

        record, _ = uow.publications.reserve(intent)
        if record.prepared_head is not None:
            if record.prepared_head != prepared:
                raise IdempotencyConflictError("publication already has a different prepared head")
            return record.prepared_head
        if record.state is not PublicationState.RESERVED:
            raise IdempotencyConflictError("publication is not reservable for preparation")
        return uow.publications.record_prepared(prepared, record.version).prepared_head

    def _validate_preconditions(self, uow: UnitOfWork, intent: PublicationIntent):
        fuse = uow.fuse.get()
        if fuse.engaged:
            raise ValidationError("publication preparation is blocked by the fuse")

        task = uow.tasks.get(intent.task_id)
        if task is None:
            raise TaskNotFoundError("publication task does not exist")
        if task.status is not TaskStatus.READY_TO_PUBLISH:
            raise ValidationError("publication task is not ready to publish")
        if task.risk.effective is RiskClass.R3 or task.risk.governance_revision is None:
            raise ValidationError("publication risk state is not eligible")
        if task.version != intent.expected_task_version or len(uow.events.read(intent.task_id)) != intent.expected_sequence:
            raise ValidationError("publication task version or event sequence is stale")
        if task.repository.name != intent.repository_name or task.repository.revision != intent.base_sha:
            raise ValidationError("publication repository or base SHA does not match the task")

        self._authorize(intent.governance_revision)

        result = uow.worker_results.get(intent.worker_result_id)
        session = None if result is None else uow.runner_sessions.get(result.session_id)
        if (result is None or session is None
                or result.task_id != intent.task_id
                or result.worker.role is not Role.IMPLEMENTER
                or result.target is not TaskStatus.READY_TO_PUBLISH
                or session.request.task_id != intent.task_id
                or session.request.role is not Role.IMPLEMENTER
                or session.state is not RunnerSessionState.SUCCEEDED):
            raise ValidationError("publication implementer result is not a successful canonical result")

        artifact = uow.artifacts.get(intent.patch_digest)
        if (artifact is None
                or artifact.reference not in task.artifacts
                or artifact.reference.digest.value != intent.patch_digest
                or artifact.reference.media_type != "text/x-diff"
                or artifact.source_kind != f"worker_result:{intent.worker_result_id}"):
            raise ValidationError("publication patch artifact is not the canonical Task-attached diff")
        return task

    def _authorize(self, revision: str) -> None:
        try:
            self._governance.read_revision(revision)
            if not self._governance.authorize(revision, "prepare_publication"):
                raise UnsupportedGovernanceError("publication preparation is not authorized")
        except UnsupportedGovernanceError:
            raise
        except Exception as error:
            raise UnsupportedGovernanceError("publication preparation governance is unavailable") from error


class PublicationApprovalService:
    """Bind one successful canonical PATCH_REVIEWER result to the exact verified head."""

    def approve(
        self,
        uow: UnitOfWork,
        publication_id: str,
        approval_id: str,
        reviewer_result_id: str,
        approval_artifact_digest: str,
        decided_at,
    ) -> tuple[PublicationReviewDecision, bool]:
        if uow.fuse.get().engaged:
            raise ValidationError("publication approval is blocked by the fuse")

        record = uow.publications.get(publication_id)
        if (record is None
                or record.state is not PublicationState.VERIFIED
                or record.prepared_head is None
                or record.verification is None
                or record.verification.status is not VerificationStatus.SUCCEEDED
                or record.verification.result_code is not VerificationResultCode.PASSED
                or record.verification.head_sha != record.prepared_head.head_sha):
            raise ValidationError("publication is not successfully verified")

        task = uow.tasks.get(record.intent.task_id)
        if (task is None
                or task.status is not TaskStatus.READY_TO_PUBLISH
                or task.risk.effective is RiskClass.R3
                or task.repository.name != record.intent.repository_name
                or task.repository.revision != record.intent.base_sha):
            raise ValidationError("publication task is not eligible for approval")

        result = uow.worker_results.get(reviewer_result_id)
        session = None if result is None else uow.runner_sessions.get(result.session_id)
        patch_artifact = uow.artifacts.get(record.intent.patch_digest)
        if (result is None or session is None or patch_artifact is None
                or result.task_id != record.intent.task_id
                or result.worker.role is not Role.PATCH_REVIEWER
                or result.target is not TaskStatus.READY_TO_PUBLISH
                or session.request.task_id != record.intent.task_id
                or session.request.role is not Role.PATCH_REVIEWER
                or session.state is not RunnerSessionState.SUCCEEDED
                or result.session_id != session.request.session_id
                or patch_artifact.reference not in session.request.input_artifacts):
            raise ValidationError("publication reviewer result is not a successful canonical PATCH_REVIEWER result")

        approval_artifact = uow.artifacts.get(approval_artifact_digest)
        if (approval_artifact is None
                or approval_artifact.source_kind != f"worker_result:{reviewer_result_id}"):
            raise ValidationError("publication approval artifact is not owned by the reviewer result")

        prepared = record.prepared_head
        decision = PublicationReviewDecision(
            approval_id,
            publication_id,
            record.intent.task_id,
            result.session_id,
            result.result_id,
            approval_artifact.reference,
            record.intent.patch_digest,
            prepared.base_sha,
            prepared.tree_sha,
            prepared.head_sha,
            result.worker.worker_id,
            ReviewDecision.APPROVED,
            decided_at,
        )
        return uow.publication_approvals.append(decision)


class PublicationPublishingService:
    """Persist publish intent before remote I/O, then reconcile in a new transaction."""

    def __init__(self, uow_factory, publisher) -> None:
        self._uow_factory = uow_factory
        self._publisher = publisher

    def publish(self, publication_id: str, effect_id: str) -> PublicationReceipt:
        with self._uow_factory() as uow:
            if uow.fuse.get().engaged:
                raise ValidationError("publication is blocked by the fuse")

            record = uow.publications.get(publication_id)
            if (record is None
                    or record.state is not PublicationState.VERIFIED
                    or record.prepared_head is None
                    or record.verification is None
                    or record.verification.status is not VerificationStatus.SUCCEEDED
                    or record.verification.result_code is not VerificationResultCode.PASSED
                    or record.verification.head_sha != record.prepared_head.head_sha):
                raise ValidationError("publication is not successfully verified")

            task = uow.tasks.get(record.intent.task_id)
            prepared = record.prepared_head
            if (task is None
                    or task.status is not TaskStatus.READY_TO_PUBLISH
                    or task.risk.effective is RiskClass.R3
                    or task.repository.name != record.intent.repository_name
                    or task.repository.revision != record.intent.base_sha
                    or prepared.deterministic_ref != f"refs/heads/meta-loop/{publication_id}"):
                raise ValidationError("publication task or branch is not eligible for publication")

            approvals = uow.publication_approvals.read(publication_id, prepared.head_sha)
            if not any(
                approval.decision is ReviewDecision.APPROVED
                and approval.task_id == record.intent.task_id
                and approval.patch_digest == record.intent.patch_digest
                and approval.base_sha == prepared.base_sha
                and approval.tree_sha == prepared.tree_sha
                and approval.head_sha == prepared.head_sha
                for approval in approvals
            ):
                raise ValidationError("publication requires an exact-head approval")

            effect_intent = PublicationEffectIntent(
                effect_id,
                publication_id,
                prepared.head_sha,
                prepared.deterministic_ref,
            )
            effect, _ = uow.publication_effects.request(effect_intent)
            if effect.receipt is not None:
                return effect.receipt

            immutable_intent = record.intent
            immutable_prepared = prepared
            immutable_effect = effect.intent
            uow.commit()

        receipt = self._publisher.reconcile(immutable_intent, immutable_prepared, immutable_effect)
        self._validate_receipt(receipt, immutable_prepared, immutable_effect)

        with self._uow_factory() as uow:
            current = uow.publication_effects.get(effect_id)
            if current is None or current.intent.canonical() != immutable_effect.canonical():
                raise IdempotencyConflictError("durable publication effect drifted before reconciliation")
            if current.receipt is not None:
                if current.receipt.canonical() != receipt.canonical():
                    raise IdempotencyConflictError("publication effect has conflicting remote receipt")
                return current.receipt
            reconciled = uow.publication_effects.reconcile(receipt)
            uow.commit()
            if reconciled.receipt is None:
                raise ValidationError("publication reconciliation did not persist a receipt")
            return reconciled.receipt

    @staticmethod
    def _validate_receipt(receipt: PublicationReceipt, prepared: PreparedHead, effect: PublicationEffectIntent) -> None:
        pull_request = receipt.pull_request
        if (not isinstance(receipt, PublicationReceipt)
                or receipt.publication_id != effect.publication_id
                or receipt.effect_id != effect.effect_id
                or receipt.state is not PublicationEffectState.RECONCILED
                or pull_request is None
                or pull_request.publication_id != effect.publication_id
                or pull_request.base_sha != prepared.base_sha
                or pull_request.tree_sha != prepared.tree_sha
                or pull_request.head_sha != prepared.head_sha):
            raise IdempotencyConflictError("remote publication receipt does not match the prepared head")


class PublicationCheckService:
    """Read current exact-head checks outside the UoW, then persist normalized observations."""

    def __init__(self, uow_factory, check_source, required_checks: tuple[str, ...]) -> None:
        self._evaluator = RequiredCheckEvaluator(required_checks)
        self._uow_factory = uow_factory
        self._check_source = check_source
        self._required_checks = required_checks

    def check(self, publication_id: str, effect_id: str) -> CheckGateResult:
        with self._uow_factory() as uow:
            record, effect = self._validated_state(uow, publication_id, effect_id)
            prepared = record.prepared_head
            repository_name = record.intent.repository_name
            head_sha = prepared.head_sha
            effect_canonical = effect.intent.canonical()

        observations = self._check_source.read(publication_id, repository_name, head_sha)
        if not isinstance(observations, tuple):
            raise ValidationError("GitHub check observations are invalid")
        for observation in observations:
            if not isinstance(observation, CheckRunObservation) or observation.publication_id != publication_id:
                raise ValidationError("GitHub check observation does not match publication identity")
        evaluation = self._evaluator.evaluate(observations, head_sha)

        with self._uow_factory() as uow:
            record, effect = self._validated_state(uow, publication_id, effect_id)
            if record.prepared_head.head_sha != head_sha or effect.intent.canonical() != effect_canonical:
                raise IdempotencyConflictError("publication changed while check observations were fetched")
            for observation in observations:
                uow.check_observations.append(observation)
            uow.commit()

        return CheckGateResult(
            publication_id,
            head_sha,
            self._required_checks,
            evaluation.status is RequiredCheckStatus.PASSED,
        )

    @staticmethod
    def _validated_state(uow: UnitOfWork, publication_id: str, effect_id: str):
        if uow.fuse.get().engaged:
            raise ValidationError("publication checks are blocked by the fuse")
        record = uow.publications.get(publication_id)
        effect = uow.publication_effects.get(effect_id)
        if (record is None or record.prepared_head is None or record.verification is None
                or record.state is not PublicationState.VERIFIED
                or record.verification.status is not VerificationStatus.SUCCEEDED
                or record.verification.result_code is not VerificationResultCode.PASSED
                or record.verification.head_sha != record.prepared_head.head_sha):
            raise ValidationError("publication is not successfully verified")
        receipt = None if effect is None else effect.receipt
        pull_request = None if receipt is None else receipt.pull_request
        if (effect is None or receipt is None or pull_request is None
                or receipt.state is not PublicationEffectState.RECONCILED
                or effect.intent.publication_id != publication_id
                or effect.intent.head_sha != record.prepared_head.head_sha
                or effect.intent.deterministic_ref != record.prepared_head.deterministic_ref
                or receipt.publication_id != publication_id
                or pull_request.head_sha != record.prepared_head.head_sha
                or pull_request.tree_sha != record.prepared_head.tree_sha
                or pull_request.base_sha != record.prepared_head.base_sha):
            raise ValidationError("publication effect is not reconciled to the exact publication head")
        return record, effect
