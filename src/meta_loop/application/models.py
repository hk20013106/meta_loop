from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json

from meta_loop.domain.artifacts import ArtifactRef
from meta_loop.domain.enums import Role, TaskStatus
from meta_loop.domain.errors import ValidationError


@dataclass(frozen=True)
class Lease:
    task_id: str
    worker_id: str
    expires_at: datetime
    attempt: int


@dataclass(frozen=True)
class QueueEnqueueResult:
    task_id: str
    created: bool


@dataclass(frozen=True)
class QueueCompletionResult:
    task_id: str
    terminal: bool


@dataclass(frozen=True)
class QueueFailureResult:
    task_id: str
    retry_at: datetime | None
    terminal: bool


@dataclass(frozen=True)
class FuseState:
    engaged: bool
    changed_at: datetime
    governance_revision: str | None = None


@dataclass(frozen=True)
class IntakeRecord:
    request_id: str
    canonical_request: str
    task_id: str


@dataclass(frozen=True)
class IssueIngestionRecord:
    source_key: str
    canonical_request: str
    task_id: str
    trigger_event_id: str


@dataclass(frozen=True)
class IssueIngestionReceipt:
    task_id: str
    created: bool


@dataclass(frozen=True)
class ControlEvent:
    action: str
    occurred_at: datetime
    governance_revision: str | None = None


@dataclass(frozen=True)
class CatalogedArtifact:
    reference: ArtifactRef
    classification: str
    source_kind: str
    size_bytes: int


class RunnerSessionState(str, Enum):
    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunnerSessionRequest:
    session_id: str
    task_id: str
    role: Role
    expected_task_version: int
    expected_sequence: int
    input_artifacts: tuple[ArtifactRef, ...]
    timeout_seconds: int
    correlation_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.session_id or not self.task_id or not self.correlation_id:
            raise ValidationError("runner session identifiers and schema version are required")
        if self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("runner role is not supported")
        if self.expected_task_version < 0 or self.expected_sequence < 0 or not 1 <= self.timeout_seconds <= 3600:
            raise ValidationError("runner session versions and timeout are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "expected_task_version": self.expected_task_version,
            "expected_sequence": self.expected_sequence,
            "input_artifacts": [artifact.to_dict() for artifact in self.input_artifacts],
            "timeout_seconds": self.timeout_seconds,
            "correlation_id": self.correlation_id,
        }

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunnerSessionOutcome:
    session_id: str
    state: RunnerSessionState
    summary: str

    def __post_init__(self) -> None:
        if not self.session_id or self.state is RunnerSessionState.REQUESTED or not self.summary or len(self.summary) > 512:
            raise ValidationError("runner outcome is invalid")


@dataclass(frozen=True)
class RunnerSessionRecord:
    request: RunnerSessionRequest
    state: RunnerSessionState = RunnerSessionState.REQUESTED
    outcome: RunnerSessionOutcome | None = None


@dataclass(frozen=True)
class RunnerSessionReceipt:
    session_id: str
    state: RunnerSessionState
    created: bool


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.worker_id or self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("worker identity is not authorized for a worker role")


@dataclass(frozen=True)
class WorkerResult:
    result_id: str
    session_id: str
    task_id: str
    worker: WorkerIdentity
    target: TaskStatus
    summary: str
    expected_task_version: int
    expected_sequence: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.result_id or not self.session_id or not self.task_id or not self.summary or len(self.summary) > 512:
            raise ValidationError("worker result is invalid")
        if not isinstance(self.target, TaskStatus) or self.expected_task_version < 0 or self.expected_sequence < 0 or self.schema_version != 1:
            raise ValidationError("worker result version is invalid")

    def canonical(self) -> str:
        return json.dumps({"schema_version": 1, "result_id": self.result_id, "session_id": self.session_id, "task_id": self.task_id, "worker_id": self.worker.worker_id, "role": self.worker.role.value, "target": self.target.value, "summary": self.summary, "expected_task_version": self.expected_task_version, "expected_sequence": self.expected_sequence}, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkerResultReceipt:
    result_id: str
    created: bool


class WorkspacePurpose(str, Enum):
    PLANNING = "planning"
    REVIEW = "review"
    PROPOSAL = "proposal"
    IMPLEMENTATION = "implementation"


class WorkspaceState(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"


@dataclass(frozen=True)
class WorkspaceRequest:
    allocation_id: str
    task_id: str
    role: Role
    purpose: WorkspacePurpose
    source_revision: str
    expected_task_version: int
    expected_sequence: int
    correlation_id: str
    governance_revision: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (not self.allocation_id or not self.task_id or not self.correlation_id
                or self.schema_version != 1 or self.expected_task_version < 0
                or self.expected_sequence < 0):
            raise ValidationError("workspace request identifiers and versions are invalid")
        if self.role not in (Role.PLANNER, Role.DESIGN_REVIEWER, Role.IMPLEMENTER, Role.PATCH_REVIEWER):
            raise ValidationError("workspace role is not supported")
        if not isinstance(self.purpose, WorkspacePurpose):
            raise ValidationError("workspace purpose is invalid")
        if len(self.source_revision) < 40 or any(character not in "0123456789abcdef" for character in self.source_revision.lower()):
            raise ValidationError("workspace source revision must be a full hexadecimal Git revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "allocation_id": self.allocation_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "purpose": self.purpose.value,
            "source_revision": self.source_revision,
            "expected_task_version": self.expected_task_version,
            "expected_sequence": self.expected_sequence,
            "correlation_id": self.correlation_id,
            "governance_revision": self.governance_revision,
        }

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkspaceRecord:
    request: WorkspaceRequest
    read_only: bool
    repository_name: str
    state: WorkspaceState = WorkspaceState.ACTIVE

    @property
    def workspace_id(self) -> str:
        return self.request.allocation_id


@dataclass(frozen=True)
class WorkspaceReceipt:
    allocation_id: str
    state: WorkspaceState
    created: bool


class PublicationState(str, Enum):
    RESERVED = "reserved"
    PREPARED = "prepared"
    VERIFIED = "verified"
    PUBLISH_REQUESTED = "publish_requested"
    PUBLISHED = "published"


class VerificationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VerifierProfile(str, Enum):
    DEFAULT = "default"


class VerificationResultCode(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


class CheckRunStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CheckRunConclusion(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PublicationEffectState(str, Enum):
    REQUESTED = "requested"
    RECONCILED = "reconciled"


def _phase8_canonical(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _phase8_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or not value[0].isalnum() or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in value):
        raise ValidationError(f"{label} is invalid")


def _phase8_sha(value: str, label: str, length: int = 40) -> None:
    if not isinstance(value, str) or len(value) != length or any(character not in "0123456789abcdef" for character in value):
        raise ValidationError(f"{label} is invalid")


def _phase8_publication_ref(publication_id: str) -> str:
    return f"refs/heads/meta-loop/{publication_id}"


def _phase8_ref_is_known(publication_id: str, value: str) -> bool:
    return value in {_phase8_publication_ref(publication_id), f"refs/meta-loop/{publication_id}"}


@dataclass(frozen=True)
class PublicationIntent:
    publication_id: str
    task_id: str
    worker_result_id: str
    patch_digest: str
    repository_name: str
    base_sha: str
    expected_task_version: int
    expected_sequence: int
    correlation_id: str
    governance_revision: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (("publication id", self.publication_id), ("task id", self.task_id), ("worker result id", self.worker_result_id), ("correlation id", self.correlation_id), ("governance revision", self.governance_revision)):
            _phase8_identifier(value, label)
        if not isinstance(self.repository_name, str) or len(self.repository_name) > 128:
            raise ValidationError("repository name is invalid")
        repository_parts = self.repository_name.split("/")
        if len(repository_parts) != 2 or any(not part or ".." in part or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in part) for part in repository_parts):
            raise ValidationError("repository name is invalid")
        _phase8_sha(self.patch_digest, "patch digest", 64)
        _phase8_sha(self.base_sha, "base SHA")
        if self.expected_task_version < 0 or self.expected_sequence < 0 or self.schema_version != 1:
            raise ValidationError("publication intent version is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "task_id": self.task_id, "worker_result_id": self.worker_result_id, "patch_digest": self.patch_digest, "repository_name": self.repository_name, "base_sha": self.base_sha, "expected_task_version": self.expected_task_version, "expected_sequence": self.expected_sequence, "correlation_id": self.correlation_id, "governance_revision": self.governance_revision}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PreparedHead:
    publication_id: str
    patch_digest: str
    base_sha: str
    tree_sha: str
    head_sha: str
    deterministic_ref: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_sha(self.patch_digest, "patch digest", 64)
        for label, value in (("base SHA", self.base_sha), ("tree SHA", self.tree_sha), ("head SHA", self.head_sha)):
            _phase8_sha(value, label)
        if not _phase8_ref_is_known(self.publication_id, self.deterministic_ref) or self.schema_version != 1:
            raise ValidationError("deterministic ref is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "patch_digest": self.patch_digest, "base_sha": self.base_sha, "tree_sha": self.tree_sha, "head_sha": self.head_sha, "deterministic_ref": self.deterministic_ref}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class VerificationResult:
    publication_id: str
    head_sha: str
    verifier_image_digest: str
    verifier_profile: VerifierProfile
    status: VerificationStatus
    result_code: VerificationResultCode
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_sha(self.head_sha, "head SHA")
        if not isinstance(self.verifier_image_digest, str) or not self.verifier_image_digest.startswith("sha256:"):
            raise ValidationError("verifier image digest is invalid")
        _phase8_sha(self.verifier_image_digest.removeprefix("sha256:"), "verifier image digest", 64)
        if not isinstance(self.verifier_profile, VerifierProfile) or not isinstance(self.status, VerificationStatus) or not isinstance(self.result_code, VerificationResultCode) or self.schema_version != 1:
            raise ValidationError("verification result is invalid")
        if (self.status is VerificationStatus.SUCCEEDED) != (self.result_code is VerificationResultCode.PASSED):
            raise ValidationError("verification status and result code are inconsistent")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "head_sha": self.head_sha, "verifier_image_digest": self.verifier_image_digest, "verifier_profile": self.verifier_profile.value, "status": self.status.value, "result_code": self.result_code.value}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PublicationReviewDecision:
    approval_id: str
    publication_id: str
    task_id: str
    session_id: str
    result_id: str
    approval_artifact: ArtifactRef
    patch_digest: str
    base_sha: str
    tree_sha: str
    head_sha: str
    reviewer_id: str
    decision: ReviewDecision
    decided_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (("approval id", self.approval_id), ("publication id", self.publication_id), ("task id", self.task_id), ("session id", self.session_id), ("result id", self.result_id), ("reviewer id", self.reviewer_id)):
            _phase8_identifier(value, label)
        if not isinstance(self.approval_artifact, ArtifactRef):
            raise ValidationError("approval artifact is invalid")
        _phase8_sha(self.patch_digest, "patch digest", 64)
        for label, value in (("base SHA", self.base_sha), ("tree SHA", self.tree_sha), ("head SHA", self.head_sha)):
            _phase8_sha(value, label)
        if not isinstance(self.decision, ReviewDecision) or not isinstance(self.decided_at, datetime) or self.schema_version != 1:
            raise ValidationError("publication review decision is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "approval_id": self.approval_id, "publication_id": self.publication_id, "task_id": self.task_id, "session_id": self.session_id, "result_id": self.result_id, "approval_artifact_digest": self.approval_artifact.digest.value, "patch_digest": self.patch_digest, "base_sha": self.base_sha, "tree_sha": self.tree_sha, "head_sha": self.head_sha, "reviewer_id": self.reviewer_id, "decision": self.decision.value, "decided_at": self.decided_at.isoformat()}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PullRequestReceipt:
    publication_id: str
    pull_request_number: int
    base_sha: str
    tree_sha: str
    head_sha: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        if not isinstance(self.pull_request_number, int) or isinstance(self.pull_request_number, bool) or self.pull_request_number <= 0:
            raise ValidationError("pull request number is invalid")
        for label, value in (("base SHA", self.base_sha), ("tree SHA", self.tree_sha), ("head SHA", self.head_sha)):
            _phase8_sha(value, label)
        if self.schema_version != 1:
            raise ValidationError("pull request receipt is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "pull_request_number": self.pull_request_number, "base_sha": self.base_sha, "tree_sha": self.tree_sha, "head_sha": self.head_sha}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PublicationReceipt:
    publication_id: str
    effect_id: str
    state: PublicationEffectState
    pull_request: PullRequestReceipt | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_identifier(self.effect_id, "effect id")
        if not isinstance(self.state, PublicationEffectState) or self.schema_version != 1:
            raise ValidationError("publication receipt is invalid")
        if self.pull_request is not None and (not isinstance(self.pull_request, PullRequestReceipt) or self.pull_request.publication_id != self.publication_id):
            raise ValidationError("publication receipt pull request is invalid")
        if self.state is PublicationEffectState.RECONCILED and self.pull_request is None:
            raise ValidationError("reconciled publication receipt requires a pull request")
        if self.state is PublicationEffectState.REQUESTED and self.pull_request is not None:
            raise ValidationError("requested publication receipt cannot contain a pull request")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "effect_id": self.effect_id, "state": self.state.value, "pull_request": None if self.pull_request is None else self.pull_request.to_dict()}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class CheckRunObservation:
    publication_id: str
    head_sha: str
    check_name: str
    status: CheckRunStatus
    conclusion: CheckRunConclusion | None
    observed_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_sha(self.head_sha, "head SHA")
        _phase8_identifier(self.check_name, "check name")
        if not isinstance(self.status, CheckRunStatus) or (self.conclusion is not None and not isinstance(self.conclusion, CheckRunConclusion)) or not isinstance(self.observed_at, datetime) or self.schema_version != 1:
            raise ValidationError("check run observation is invalid")
        if self.status is CheckRunStatus.COMPLETED and self.conclusion is None:
            raise ValidationError("completed check observation requires a conclusion")
        if self.status is not CheckRunStatus.COMPLETED and self.conclusion is not None:
            raise ValidationError("incomplete check observation cannot have a conclusion")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "head_sha": self.head_sha, "check_name": self.check_name, "status": self.status.value, "conclusion": None if self.conclusion is None else self.conclusion.value, "observed_at": self.observed_at.isoformat()}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class CheckGateResult:
    publication_id: str
    head_sha: str
    required_checks: tuple[str, ...]
    passed: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_sha(self.head_sha, "head SHA")
        if not isinstance(self.required_checks, tuple) or not isinstance(self.passed, bool) or self.schema_version != 1:
            raise ValidationError("check gate result is invalid")
        for check in self.required_checks:
            _phase8_identifier(check, "required check")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "publication_id": self.publication_id, "head_sha": self.head_sha, "required_checks": list(self.required_checks), "passed": self.passed}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PublicationRecord:
    intent: PublicationIntent
    state: PublicationState = PublicationState.RESERVED
    version: int = 0
    prepared_head: PreparedHead | None = None
    verification: VerificationResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, PublicationIntent) or not isinstance(self.state, PublicationState) or self.version < 0:
            raise ValidationError("publication record is invalid")
        if self.prepared_head is not None and (not isinstance(self.prepared_head, PreparedHead) or self.prepared_head.publication_id != self.intent.publication_id):
            raise ValidationError("publication prepared head is invalid")
        if self.verification is not None and (not isinstance(self.verification, VerificationResult) or self.prepared_head is None or self.verification.head_sha != self.prepared_head.head_sha):
            raise ValidationError("publication verification is invalid")


@dataclass(frozen=True)
class PublicationEffectIntent:
    effect_id: str
    publication_id: str
    head_sha: str
    deterministic_ref: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _phase8_identifier(self.effect_id, "effect id")
        _phase8_identifier(self.publication_id, "publication id")
        _phase8_sha(self.head_sha, "head SHA")
        if not _phase8_ref_is_known(self.publication_id, self.deterministic_ref) or self.schema_version != 1:
            raise ValidationError("publication effect intent is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "effect_id": self.effect_id, "publication_id": self.publication_id, "head_sha": self.head_sha, "deterministic_ref": self.deterministic_ref}

    def canonical(self) -> str:
        return _phase8_canonical(self.to_dict())


@dataclass(frozen=True)
class PublicationEffectRecord:
    intent: PublicationEffectIntent
    receipt: PublicationReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, PublicationEffectIntent):
            raise ValidationError("publication effect record is invalid")
        if self.receipt is not None and (not isinstance(self.receipt, PublicationReceipt) or self.receipt.publication_id != self.intent.publication_id or self.receipt.effect_id != self.intent.effect_id):
            raise ValidationError("publication effect receipt is invalid")
