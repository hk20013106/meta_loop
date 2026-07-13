"""Application coordination for managed Git workspaces."""

from dataclasses import replace

from meta_loop.application.models import WorkspaceReceipt, WorkspacePurpose, WorkspaceRecord, WorkspaceRequest, WorkspaceState
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, OptimisticConflictError, TaskNotFoundError, UnsupportedGovernanceError, ValidationError
from meta_loop.domain.events import Event


class FakeWorkspaceManager:
    """Deterministic manager that never creates a filesystem path."""

    def __init__(self) -> None:
        self._records: dict[str, WorkspaceRecord] = {}

    def allocate(self, request, repository, read_only: bool) -> WorkspaceRecord:
        current = self._records.get(request.allocation_id)
        if current is not None:
            if current.request.canonical() != request.canonical():
                raise IdempotencyConflictError("workspace allocation id has conflicting content")
            return current
        record = WorkspaceRecord(request, read_only, repository.name)
        self._records[record.workspace_id] = record
        return record

    def release(self, record: WorkspaceRecord) -> bool:
        current = self._records.get(record.workspace_id)
        if current is None or current.state is WorkspaceState.RELEASED:
            return False
        self._records[record.workspace_id] = replace(current, state=WorkspaceState.RELEASED)
        return True


class WorkspaceAllocationService:
    def __init__(self, clock, ids, manager, governance=None) -> None:
        self._clock, self._ids, self._manager, self._governance = clock, ids, manager, governance

    def allocate(self, uow, request: WorkspaceRequest) -> WorkspaceReceipt:
        existing = uow.workspaces.get(request.allocation_id)
        if existing is not None:
            if existing.request.canonical() != request.canonical():
                raise IdempotencyConflictError("workspace allocation id has conflicting content")
            return WorkspaceReceipt(existing.workspace_id, existing.state, False)
        if uow.fuse.get().engaged:
            raise ValidationError("fuse is engaged")
        task = uow.tasks.get(request.task_id)
        if task is None:
            raise TaskNotFoundError("workspace task does not exist")
        if task.version != request.expected_task_version or len(uow.events.read(task.task_id)) != request.expected_sequence:
            raise OptimisticConflictError("workspace task or event version does not match")
        read_only = self._validate_policy(task, request)
        record = self._manager.allocate(request, task.repository, read_only)
        try:
            stored, created = uow.workspaces.record_once(record)
            if created:
                uow.events.append(Event(self._ids.new(), task.task_id, EventType.WORKSPACE_ALLOCATED, self._clock.now(), Role.SYSTEM, {"workspace_id": stored.workspace_id, "role": request.role.value, "purpose": request.purpose.value, "source_revision": request.source_revision}, 1, None, request.correlation_id, request.expected_sequence + 1), request.expected_sequence)
        except Exception:
            self._manager.release(record)
            raise
        return WorkspaceReceipt(stored.workspace_id, stored.state, created)

    def release(self, uow, allocation_id: str) -> bool:
        record = uow.workspaces.get(allocation_id)
        if record is None or record.state is WorkspaceState.RELEASED:
            return False
        if not self._manager.release(record):
            return False
        released = uow.workspaces.release(allocation_id)
        sequence = len(uow.events.read(released.request.task_id))
        uow.events.append(Event(self._ids.new(), released.request.task_id, EventType.WORKSPACE_RELEASED, self._clock.now(), Role.SYSTEM, {"workspace_id": released.workspace_id}, 1, None, released.request.correlation_id, sequence + 1), sequence)
        return True

    def _validate_policy(self, task, request: WorkspaceRequest) -> bool:
        if task.risk.effective is RiskClass.R3:
            if request.role is Role.IMPLEMENTER or request.purpose is not WorkspacePurpose.PROPOSAL:
                raise ValidationError("R3 tasks require a read-only proposal workspace")
            return True
        if request.role is Role.IMPLEMENTER:
            if request.purpose is not WorkspacePurpose.IMPLEMENTATION or task.status is not TaskStatus.IMPLEMENTING:
                raise ValidationError("implementer workspaces require implementation state and purpose")
            self._authorize_implementation(request)
            return False
        if request.role is Role.PLANNER and request.purpose is WorkspacePurpose.PLANNING:
            return True
        if request.role in (Role.DESIGN_REVIEWER, Role.PATCH_REVIEWER) and request.purpose is WorkspacePurpose.REVIEW:
            return True
        raise ValidationError("workspace purpose does not match its role")

    def _authorize_implementation(self, request: WorkspaceRequest) -> None:
        if self._governance is None or not request.governance_revision:
            raise UnsupportedGovernanceError("implementation workspace requires governance authorization")
        try:
            self._governance.read_revision(request.governance_revision)
            if not self._governance.authorize(request.governance_revision, "workspace_implementation"):
                raise UnsupportedGovernanceError("implementation workspace is not authorized")
        except UnsupportedGovernanceError:
            raise
        except Exception as error:
            raise UnsupportedGovernanceError("implementation workspace is not authorized") from error
