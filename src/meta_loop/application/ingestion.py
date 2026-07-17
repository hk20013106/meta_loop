"""Provider-neutral Issue task ingestion; providers supply verified candidates."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re

from meta_loop.application.artifacts import ArtifactRegistrationService, ArtifactRequest
from meta_loop.application.controller import IntakeRequest, TaskIntakeService
from meta_loop.application.models import IssueIngestionReceipt, IssueIngestionRecord
from meta_loop.domain.enums import EventType, RiskClass, Role
from meta_loop.domain.errors import IdempotencyConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError
from meta_loop.domain.events import Event


_FENCE = re.compile(r"```meta-loop-json\s*\n(.*?)```", re.DOTALL)
_SECRET_MARKERS = ("github_pat_", "ghp_", "-----begin private key-----", "aws_secret_access_key")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
GITHUB_ISSUE_INGESTION_CAPABILITY = "github_issue_ingestion"


@dataclass(frozen=True)
class IssueTaskSpec:
    objective: str
    acceptance_criteria: tuple[str, ...]
    revision: str
    risk: RiskClass

    def __post_init__(self) -> None:
        if not isinstance(self.objective, str) or not self.objective.strip() or len(self.objective) > 4096 or not isinstance(self.acceptance_criteria, tuple) or not 1 <= len(self.acceptance_criteria) <= 20:
            raise ValidationError("issue task specification is invalid")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in self.acceptance_criteria):
            raise ValidationError("issue acceptance criteria are invalid")
        if not isinstance(self.revision, str) or _SHA.fullmatch(self.revision) is None:
            raise ValidationError("issue revision must be a full hexadecimal Git SHA")
        if type(self.risk) is not RiskClass:
            raise ValidationError("issue risk is invalid")

    def canonical(self) -> str:
        return json.dumps({"schema_version": 1, "objective": self.objective, "acceptance_criteria": list(self.acceptance_criteria), "revision": self.revision, "risk": self.risk.value}, sort_keys=True, separators=(",", ":"))


def parse_issue_task_spec(body: str) -> IssueTaskSpec:
    if not isinstance(body, str) or len(body) > 65536:
        raise ValidationError("issue body is invalid")
    blocks = _FENCE.findall(body)
    if len(blocks) != 1:
        raise ValidationError("issue body must contain exactly one meta-loop-json contract")
    try:
        def object_without_duplicate_keys(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        value = json.loads(blocks[0], object_pairs_hook=object_without_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error) == "duplicate key":
            raise ValidationError("issue task contract has duplicate fields") from error
        raise ValidationError("issue task contract is not valid JSON") from error
    allowed = {"schema_version", "objective", "acceptance_criteria", "revision", "risk"}
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValidationError("issue task contract has unknown or missing fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError("issue task contract schema version is unsupported")
    if not isinstance(value["objective"], str) or not isinstance(value["acceptance_criteria"], list) or not all(isinstance(item, str) for item in value["acceptance_criteria"]) or not isinstance(value["revision"], str) or not isinstance(value["risk"], str):
        raise ValidationError("issue task contract fields are invalid")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).casefold()
    if any(marker in canonical for marker in _SECRET_MARKERS):
        raise ValidationError("issue task contract contains a credential-like value")
    try:
        return IssueTaskSpec(value["objective"], tuple(value["acceptance_criteria"]), value["revision"], RiskClass(value["risk"]))
    except (TypeError, ValueError) as error:
        raise ValidationError("issue task contract fields are invalid") from error


@dataclass(frozen=True)
class IssueCandidate:
    provider: str
    source_key: str
    source_reference: str
    trigger_event_id: str
    trigger_actor: str
    trigger_label: str
    occurred_at: datetime
    spec: IssueTaskSpec

    def __post_init__(self) -> None:
        if not all((self.provider, self.source_key, self.source_reference, self.trigger_event_id, self.trigger_actor, self.trigger_label)):
            raise ValidationError("issue candidate identifiers are required")
        if "#" not in self.source_reference:
            raise ValidationError("issue source reference is invalid")

    @property
    def repository_name(self) -> str:
        return self.source_reference.split("#", 1)[0]

    def canonical(self) -> str:
        return json.dumps({"schema_version": 1, "provider": self.provider, "source_key": self.source_key, "source_reference": self.source_reference, "trigger_event_id": self.trigger_event_id, "trigger_actor": self.trigger_actor, "trigger_label": self.trigger_label, "occurred_at": self.occurred_at.isoformat(), "spec": json.loads(self.spec.canonical())}, sort_keys=True, separators=(",", ":"))


class IssueIngestionService:
    def __init__(self, artifact_store, clock, ids, governance=None, governance_revision: str | None = None) -> None:
        self._artifacts = ArtifactRegistrationService(artifact_store, ids, clock)
        self._clock, self._ids = clock, ids
        self._governance, self._governance_revision = governance, governance_revision

    def _authorize(self) -> None:
        if self._governance is None or not isinstance(self._governance_revision, str) or not self._governance_revision:
            raise UnsupportedGovernanceError("GitHub Issue ingestion requires governance authorization")
        try:
            self._governance.read_revision(self._governance_revision)
            if not self._governance.authorize(self._governance_revision, GITHUB_ISSUE_INGESTION_CAPABILITY):
                raise UnsupportedGovernanceError("GitHub Issue ingestion is not authorized")
        except UnsupportedGovernanceError:
            raise
        except Exception as error:
            raise UnsupportedGovernanceError("GitHub Issue ingestion is not authorized") from error

    def ingest(self, uow, candidate: IssueCandidate) -> IssueIngestionReceipt:
        self._authorize()
        if uow.fuse.locked_get().engaged:
            raise ValidationError("fuse is engaged")
        uow.source_ingestions.reserve(candidate.source_key, candidate.trigger_event_id)
        existing = uow.source_ingestions.get(candidate.source_key)
        if existing is not None:
            if existing.canonical_request != candidate.canonical():
                raise IdempotencyConflictError("source has conflicting canonical content")
            return IssueIngestionReceipt(existing.task_id, False)
        if uow.source_ingestions.get_by_trigger_event(candidate.trigger_event_id) is not None:
            raise IdempotencyConflictError("trigger event is already associated with another source")
        intake = IntakeRequest(candidate.source_key, "github_issue", candidate.source_reference, candidate.repository_name, candidate.spec.revision, candidate.spec.risk, candidate.spec.risk, candidate.spec.risk, governance_revision=self._governance_revision, enqueue=True)
        task = TaskIntakeService(self._clock, self._ids).start(uow, intake)
        reference = self._artifacts.register(uow, task.task_id, [candidate.spec.canonical().encode("utf-8")], ArtifactRequest("issue-task.json", "application/vnd.meta-loop.issue-task+json", "source_input", "github_issue"), task.version, len(uow.events.read(task.task_id)))
        current = uow.tasks.get(task.task_id)
        if current is None:
            raise TaskNotFoundError("ingested task disappeared")
        sequence = len(uow.events.read(task.task_id))
        uow.events.append(Event(self._ids.new(), task.task_id, EventType.SOURCE_INGESTED, self._clock.now(), Role.SYSTEM, {"source_key": candidate.source_key, "trigger_event_id": candidate.trigger_event_id, "trigger_actor": candidate.trigger_actor, "trigger_label": candidate.trigger_label, "spec_digest": reference.digest.value}, 1, None, candidate.source_key, sequence + 1), sequence)
        stored, created = uow.source_ingestions.record_once(IssueIngestionRecord(candidate.source_key, candidate.canonical(), current.task_id, candidate.trigger_event_id))
        return IssueIngestionReceipt(stored.task_id, created)
