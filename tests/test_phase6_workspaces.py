from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import stat
import subprocess

import pytest

from meta_loop.application.models import (
    WorkspacePurpose,
    WorkspaceRequest,
    WorkspaceState,
)
from meta_loop.application.workspaces import (
    FakeWorkspaceManager,
    WorkspaceAllocationService,
)
from meta_loop.domain.enums import EventType, RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, UnsupportedGovernanceError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import FixedClock, InMemoryUnitOfWork, SequentialIdGenerator
from meta_loop.infrastructure.workspaces import LocalGitWorkspaceManager


NOW = datetime(2026, 7, 14, tzinfo=UTC)
REVISION = "a" * 40


class AllowGovernance:
    def read_revision(self, revision):
        assert revision == "policy-v1"
        return {"revision": revision}

    def authorize(self, revision, capability):
        return capability == "workspace_implementation"


def make_task(risk=RiskClass.R0, status=TaskStatus.PLANNED):
    return replace(
        Task.create(
            "task-6", TaskSource("synthetic", "fixture"),
            RepositoryTarget("fixture/repo", "main"),
            RiskAssessment(risk, risk, risk), NOW,
        ),
        status=status,
    )


def request(**changes):
    values = dict(
        allocation_id="workspace-6",
        task_id="task-6",
        role=Role.PLANNER,
        purpose=WorkspacePurpose.PLANNING,
        source_revision=REVISION,
        expected_task_version=0,
        expected_sequence=0,
        correlation_id="workspace-correlation",
    )
    values.update(changes)
    return WorkspaceRequest(**values)


def test_workspace_request_is_canonical_and_excludes_paths_and_credentials():
    canonical = request().canonical()
    assert '"schema_version":1' in canonical
    assert "path" not in canonical
    assert "token" not in canonical
    assert "password" not in canonical


def test_workspace_allocation_is_atomic_and_idempotent():
    uow = InMemoryUnitOfWork()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with uow:
        uow.tasks.create(make_task())
        receipt = service.allocate(uow, request())
        uow.commit()

    assert receipt.created
    assert receipt.state is WorkspaceState.ACTIVE
    assert uow.workspaces.get("workspace-6").read_only
    assert uow.events.read("task-6")[0].event_type is EventType.WORKSPACE_ALLOCATED

    with uow:
        assert not service.allocate(uow, request()).created
        with pytest.raises(IdempotencyConflictError):
            service.allocate(uow, request(correlation_id="different"))


def test_only_one_active_workspace_per_task_and_purpose_is_allowed():
    uow = InMemoryUnitOfWork()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with uow:
        uow.tasks.create(make_task())
        service.allocate(uow, request())
        with pytest.raises(IdempotencyConflictError, match="active"):
            service.allocate(uow, request(allocation_id="workspace-6b", expected_sequence=1))


def test_implementation_workspace_requires_matching_governance_and_is_writable():
    uow = InMemoryUnitOfWork()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager(), AllowGovernance())
    implementation = request(role=Role.IMPLEMENTER, purpose=WorkspacePurpose.IMPLEMENTATION, governance_revision="policy-v1")
    with uow:
        uow.tasks.create(make_task(status=TaskStatus.IMPLEMENTING))
        receipt = service.allocate(uow, implementation)
        uow.commit()
    assert receipt.created
    assert not uow.workspaces.get(implementation.allocation_id).read_only


def test_uncommitted_workspace_allocation_rolls_back_ledger_and_event():
    uow = InMemoryUnitOfWork()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with uow:
        uow.tasks.create(make_task())
        service.allocate(uow, request())

    assert uow.workspaces.get("workspace-6") is None
    assert uow.tasks.get("task-6") is None


def test_fuse_r3_and_implementation_governance_are_fail_closed():
    uow = InMemoryUnitOfWork()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), FakeWorkspaceManager())
    with uow:
        uow.tasks.create(make_task(RiskClass.R3, TaskStatus.DESIGN_REVIEW))
        with pytest.raises(ValidationError, match="R3"):
            service.allocate(uow, request(role=Role.IMPLEMENTER, purpose=WorkspacePurpose.IMPLEMENTATION))
        with pytest.raises(ValidationError, match="proposal"):
            service.allocate(uow, request(role=Role.DESIGN_REVIEWER, purpose=WorkspacePurpose.REVIEW))
        uow.fuse.set(__import__("meta_loop.application.models", fromlist=["FuseState"]).FuseState(True, NOW))
        with pytest.raises(ValidationError, match="fuse"):
            service.allocate(uow, request(purpose=WorkspacePurpose.PROPOSAL))

    uow = InMemoryUnitOfWork()
    with uow:
        uow.tasks.create(make_task(status=TaskStatus.IMPLEMENTING))
        with pytest.raises(UnsupportedGovernanceError):
            service.allocate(uow, request(role=Role.IMPLEMENTER, purpose=WorkspacePurpose.IMPLEMENTATION))


def test_release_is_idempotent_and_does_not_expose_a_path():
    uow = InMemoryUnitOfWork()
    manager = FakeWorkspaceManager()
    service = WorkspaceAllocationService(FixedClock(NOW), SequentialIdGenerator("event"), manager)
    with uow:
        uow.tasks.create(make_task())
        service.allocate(uow, request())
        uow.commit()
    with uow:
        assert service.release(uow, "workspace-6")
        assert not service.release(uow, "workspace-6")
        uow.commit()
    record = uow.workspaces.get("workspace-6")
    assert record.state is WorkspaceState.RELEASED
    assert not hasattr(record, "path")


def run_git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_local_git_manager_uses_allowlist_fixed_revision_and_managed_root(tmp_path):
    source, managed = tmp_path / "source", tmp_path / "managed"
    source.mkdir()
    run_git("init", cwd=source)
    run_git("config", "user.email", "test@example.invalid", cwd=source)
    run_git("config", "user.name", "Meta Loop Test", cwd=source)
    (source / "README.txt").write_text("fixture", encoding="utf-8")
    run_git("add", "README.txt", cwd=source)
    run_git("commit", "-m", "fixture", cwd=source)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    manager = LocalGitWorkspaceManager({"fixture/repo": source}, managed, forbidden_roots=(source.parent / "research_loop",))
    allocated = manager.allocate(request(source_revision=revision), RepositoryTarget("fixture/repo", "main"), read_only=True)
    assert allocated.workspace_id == "workspace-6"
    assert not ((managed / allocated.workspace_id / "README.txt").stat().st_mode & stat.S_IWUSR)
    assert not (source / ".git" / "index.lock").exists()
    assert manager.release(allocated)
    assert not manager.release(allocated)


def test_local_git_manager_rejects_head_drift_and_symlink_managed_root(tmp_path, monkeypatch):
    source, managed = tmp_path / "source", tmp_path / "managed"
    source.mkdir()
    run_git("init", cwd=source)
    run_git("config", "user.email", "test@example.invalid", cwd=source)
    run_git("config", "user.name", "Meta Loop Test", cwd=source)
    (source / "README.txt").write_text("fixture", encoding="utf-8")
    run_git("add", "README.txt", cwd=source)
    run_git("commit", "-m", "fixture", cwd=source)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    (source / "README.txt").write_text("later", encoding="utf-8")
    run_git("commit", "-am", "later", cwd=source)
    later_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    manager = LocalGitWorkspaceManager({"fixture/repo": source}, managed)
    allocated = manager.allocate(request(source_revision=revision), RepositoryTarget("fixture/repo", "main"), read_only=True)
    run_git("update-ref", "HEAD", later_revision, cwd=managed / allocated.workspace_id)
    with pytest.raises(IdempotencyConflictError, match="head"):
        manager.allocate(request(source_revision=revision), RepositoryTarget("fixture/repo", "main"), read_only=True)
    manager.release(allocated)

    outside, link = tmp_path / "outside", tmp_path / "link"
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        monkeypatch.setattr(LocalGitWorkspaceManager, "_has_symlink_component", staticmethod(lambda _path: True))
        link = tmp_path / "simulated-link"
    with pytest.raises(ValidationError, match="symlink"):
        LocalGitWorkspaceManager({"fixture/repo": source}, link)


def test_workspace_application_has_no_subprocess_or_sibling_import():
    source = Path("src/meta_loop/application/workspaces.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "research_loop" not in source
