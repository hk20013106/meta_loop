"""Application gate for deterministic Phase 8 publication preparation."""

from hashlib import sha256

from meta_loop.application.models import PreparedHead, PublicationIntent, PublicationState, RunnerSessionState
from meta_loop.application.ports import CandidatePreparer, ContentAddressedBlobStore, GovernanceReader, UnitOfWork
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError


class PublicationPreparationService:
    def __init__(self, blobs: ContentAddressedBlobStore, preparer: CandidatePreparer, governance: GovernanceReader) -> None:
        self._blobs = blobs
        self._preparer = preparer
        self._governance = governance

    def prepare(self, uow: UnitOfWork, intent: PublicationIntent) -> PreparedHead:
        task = self._validate_preconditions(uow, intent)
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
                or prepared.deterministic_ref != f"refs/meta-loop/{intent.publication_id}"):
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
