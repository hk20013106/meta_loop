"""Opt-in local Git worktree adapter; no application module imports subprocess."""

from pathlib import Path
import shutil
import stat
import subprocess

from meta_loop.application.models import WorkspaceRecord, WorkspaceRequest, WorkspaceState
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError


class LocalGitWorkspaceManager:
    def __init__(self, repositories: dict[str, Path], managed_root: Path, forbidden_roots: tuple[Path, ...] = ()) -> None:
        if self._has_symlink_component(managed_root):
            raise ValidationError("managed workspace root cannot contain a symlink")
        self._repositories = {name: path.resolve() for name, path in repositories.items()}
        self._managed_root = managed_root.resolve()
        self._forbidden_roots = tuple(path.resolve() for path in forbidden_roots)
        if any(self._within(self._managed_root, root) or self._within(root, self._managed_root) for root in self._forbidden_roots):
            raise ValidationError("managed workspace root intersects a forbidden boundary")

    def allocate(self, request: WorkspaceRequest, repository, read_only: bool) -> WorkspaceRecord:
        self.checkout_owned(repository.name, request.allocation_id, request.source_revision, read_only=read_only)
        return WorkspaceRecord(request, read_only, repository.name)

    def checkout_owned(self, repository_name: str, allocation_id: str, revision: str, read_only: bool = False) -> Path:
        source = self._repository(repository_name, "workspace repository is not an allowed local Git root")
        self._managed_root.mkdir(parents=True, exist_ok=True)
        destination = self._destination(allocation_id)
        verified = self._git(source, "rev-parse", "--verify", f"{revision}^{{commit}}")
        if verified != revision:
            raise ValidationError("workspace source revision does not match the verified commit")
        if destination.exists():
            head = self._git(destination, "rev-parse", "HEAD")
            if head != revision:
                raise IdempotencyConflictError("managed workspace has conflicting head revision")
            if read_only:
                self._set_read_only(destination)
            return destination
        self._git(source, "worktree", "add", "--detach", str(destination), revision)
        if read_only:
            self._set_read_only(destination)
        return destination

    def release(self, record: WorkspaceRecord) -> bool:
        return self.release_owned(record.repository_name, record.workspace_id)

    def release_owned(self, repository_name: str, allocation_id: str) -> bool:
        destination = self._destination(allocation_id)
        if not destination.exists():
            return False
        source = self._repository(repository_name, "workspace repository configuration is unavailable")
        self._set_writable(destination)
        try:
            self._git(source, "worktree", "remove", "--force", "--force", str(destination))
        except ValidationError:
            if destination.is_symlink() or not self._within(destination.resolve(), self._managed_root):
                raise
            shutil.rmtree(destination)
            self._git(source, "worktree", "prune")
        return True

    def _repository(self, repository_name: str, message: str) -> Path:
        source = self._repositories.get(repository_name)
        if source is None or not (source / ".git").exists() or any(self._within(source, root) for root in self._forbidden_roots):
            raise ValidationError(message)
        return source

    def _destination(self, allocation_id: str) -> Path:
        if not allocation_id or any(character in allocation_id for character in "/\\") or allocation_id in (".", ".."):
            raise ValidationError("workspace allocation id is not a safe path component")
        destination = (self._managed_root / allocation_id).resolve()
        if not self._within(destination, self._managed_root):
            raise ValidationError("workspace destination escapes the managed root")
        return destination

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _has_symlink_component(path: Path) -> bool:
        current = Path(path)
        while current != current.parent:
            if current.exists() and current.is_symlink():
                return True
            current = current.parent
        return False

    def _set_read_only(self, destination: Path) -> None:
        self._set_permissions(destination, writable=False)

    def _set_writable(self, destination: Path) -> None:
        self._set_permissions(destination, writable=True)

    def _set_permissions(self, destination: Path, writable: bool) -> None:
        if destination.is_symlink() or not self._within(destination.resolve(), self._managed_root):
            raise ValidationError("workspace permission operation escapes the managed root")
        paths = [destination, *destination.rglob("*")]
        for path in sorted(paths, key=lambda value: len(value.parts), reverse=not writable):
            if path.is_symlink():
                raise ValidationError("workspace contains a symlink")
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR if writable else mode & ~stat.S_IWUSR)

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        try:
            result = subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValidationError("managed Git operation failed") from error
        return result.stdout.strip()
