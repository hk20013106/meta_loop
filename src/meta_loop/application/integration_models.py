from dataclasses import dataclass
from enum import Enum
import json

from meta_loop.domain.errors import ValidationError


class IntegrationState(str, Enum):
    MERGE_REQUESTED = "merge_requested"
    MERGED = "merged"
    CANARY_PENDING = "canary_pending"
    CANARY_PASSED = "canary_passed"
    CANARY_FAILED = "canary_failed"
    REVERT_REQUESTED = "revert_requested"
    REVERT_PUBLISHED = "revert_published"
    REVERT_CHECKED = "revert_checked"
    REVERT_MERGED = "revert_merged"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"


class CanaryStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    DRIFTED = "drifted"


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or not value[0].isalnum() or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in value):
        raise ValidationError(f"{label} is invalid")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValidationError(f"{label} is invalid")


def _repository(value: str) -> None:
    if not isinstance(value, str) or len(value) > 128:
        raise ValidationError("repository name is invalid")
    parts = value.split("/")
    if len(parts) != 2 or any(not part or ".." in part or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in part) for part in parts):
        raise ValidationError("repository name is invalid")


def _branch(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or value.startswith("/") or value.endswith("/") or ".." in value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-" for ch in value):
        raise ValidationError("target branch is invalid")


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class IntegrationIntent:
    integration_id: str
    publication_id: str
    effect_id: str
    task_id: str
    repository_name: str
    target_branch: str
    pull_request_number: int
    base_sha: str
    base_tree_sha: str
    approved_head_sha: str
    prepared_tree_sha: str
    deterministic_ref: str
    governance_revision: str
    expected_task_version: int
    expected_sequence: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (("integration id", self.integration_id), ("publication id", self.publication_id), ("effect id", self.effect_id), ("task id", self.task_id), ("governance revision", self.governance_revision)):
            _identifier(value, label)
        _repository(self.repository_name)
        _branch(self.target_branch)
        if not isinstance(self.pull_request_number, int) or self.pull_request_number < 1:
            raise ValidationError("pull request number is invalid")
        for label, value in (("base SHA", self.base_sha), ("base tree SHA", self.base_tree_sha), ("approved head SHA", self.approved_head_sha), ("prepared tree SHA", self.prepared_tree_sha)):
            _sha(value, label)
        if self.deterministic_ref != f"refs/heads/meta-loop/{self.publication_id}":
            raise ValidationError("deterministic publication ref is invalid")
        if self.expected_task_version < 0 or self.expected_sequence < 0 or self.schema_version != 1:
            raise ValidationError("integration version is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "integration_id": self.integration_id,
            "publication_id": self.publication_id,
            "effect_id": self.effect_id,
            "task_id": self.task_id,
            "repository_name": self.repository_name,
            "target_branch": self.target_branch,
            "pull_request_number": self.pull_request_number,
            "base_sha": self.base_sha,
            "base_tree_sha": self.base_tree_sha,
            "approved_head_sha": self.approved_head_sha,
            "prepared_tree_sha": self.prepared_tree_sha,
            "deterministic_ref": self.deterministic_ref,
            "governance_revision": self.governance_revision,
            "expected_task_version": self.expected_task_version,
            "expected_sequence": self.expected_sequence,
        }

    def canonical(self) -> str:
        return _canonical(self.to_dict())


@dataclass(frozen=True)
class MergeReceipt:
    integration_id: str
    pull_request_number: int
    base_sha: str
    approved_head_sha: str
    merge_sha: str
    merge_tree_sha: str
    parent_sha: str
    target_branch: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.integration_id, "integration id")
        if not isinstance(self.pull_request_number, int) or self.pull_request_number < 1:
            raise ValidationError("pull request number is invalid")
        for label, value in (("base SHA", self.base_sha), ("approved head SHA", self.approved_head_sha), ("merge SHA", self.merge_sha), ("merge tree SHA", self.merge_tree_sha), ("parent SHA", self.parent_sha)):
            _sha(value, label)
        _branch(self.target_branch)
        if self.parent_sha != self.base_sha or self.schema_version != 1:
            raise ValidationError("merge receipt does not describe an exact squash merge")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "integration_id": self.integration_id, "pull_request_number": self.pull_request_number, "base_sha": self.base_sha, "approved_head_sha": self.approved_head_sha, "merge_sha": self.merge_sha, "merge_tree_sha": self.merge_tree_sha, "parent_sha": self.parent_sha, "target_branch": self.target_branch}

    def canonical(self) -> str:
        return _canonical(self.to_dict())


@dataclass(frozen=True)
class CanaryResult:
    integration_id: str
    sha: str
    status: CanaryStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.integration_id, "integration id")
        _sha(self.sha, "canary SHA")
        if not isinstance(self.status, CanaryStatus) or self.schema_version != 1:
            raise ValidationError("canary result is invalid")

    def canonical(self) -> str:
        return _canonical({"schema_version": 1, "integration_id": self.integration_id, "sha": self.sha, "status": self.status.value})


@dataclass(frozen=True)
class RevertCandidate:
    integration_id: str
    parent_sha: str
    tree_sha: str
    head_sha: str
    deterministic_ref: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.integration_id, "integration id")
        for label, value in (("parent SHA", self.parent_sha), ("tree SHA", self.tree_sha), ("head SHA", self.head_sha)):
            _sha(value, label)
        if self.deterministic_ref != f"refs/heads/meta-loop/revert/{self.integration_id}" or self.schema_version != 1:
            raise ValidationError("revert candidate is invalid")

    def canonical(self) -> str:
        return _canonical({"schema_version": 1, "integration_id": self.integration_id, "parent_sha": self.parent_sha, "tree_sha": self.tree_sha, "head_sha": self.head_sha, "deterministic_ref": self.deterministic_ref})


@dataclass(frozen=True)
class IntegrationRecord:
    intent: IntegrationIntent
    state: IntegrationState = IntegrationState.MERGE_REQUESTED
    version: int = 0
    merge_receipt: MergeReceipt | None = None
    canary: CanaryResult | None = None
    revert_candidate: RevertCandidate | None = None
