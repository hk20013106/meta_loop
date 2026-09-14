"""Fail-closed deterministic local preparation for Phase 8 publication candidates."""

from hashlib import sha256
import os
from pathlib import Path
import shlex
import subprocess

from meta_loop.application.models import PreparedHead, PublicationIntent
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.workspaces import LocalGitWorkspaceManager


class LocalGitCandidatePreparer:
    """Prepare an exact patch on an allowlisted Git base without creating a ref."""

    _UNSAFE_PATCH_PREFIXES = (
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "old mode ",
        "new mode ",
        "similarity index ",
        "dissimilarity index ",
    )

    def __init__(self, repositories: dict[str, Path], managed_root: Path, forbidden_roots: tuple[Path, ...] = ()) -> None:
        self._worktrees = LocalGitWorkspaceManager(repositories, managed_root, forbidden_roots)

    def prepare(self, intent: PublicationIntent, patch: bytes) -> PreparedHead:
        if not isinstance(intent, PublicationIntent):
            raise ValidationError("publication intent is invalid")
        if not isinstance(patch, bytes):
            raise ValidationError("candidate patch must be bytes")
        if sha256(patch).hexdigest() != intent.patch_digest:
            raise ValidationError("candidate patch digest mismatch")
        self._validate_patch(patch)

        allocation_id = f"candidate-{intent.publication_id}"
        try:
            destination = self._worktrees.checkout_owned(intent.repository_name, allocation_id, intent.base_sha)
        except ValidationError as error:
            if "allowed local Git root" in str(error):
                raise
            raise ValidationError("candidate base SHA is not available") from error

        try:
            self._apply_patch(destination, patch)
            self._validate_index(destination)
            tree_sha = self._git(destination, "write-tree")
            head_sha = self._git(
                destination,
                "commit-tree",
                tree_sha,
                "-p",
                intent.base_sha,
                input_data=b"Meta Loop publication candidate\n",
                environment=self._deterministic_git_environment(),
            )
            return PreparedHead(
                intent.publication_id,
                intent.patch_digest,
                intent.base_sha,
                tree_sha,
                head_sha,
                f"refs/meta-loop/{intent.publication_id}",
            )
        finally:
            self._worktrees.release_owned(intent.repository_name, allocation_id)

    @classmethod
    def _validate_patch(cls, patch: bytes) -> None:
        try:
            text = patch.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("candidate patch is not valid UTF-8 text") from error
        if "\x00" in text:
            raise ValidationError("candidate patch contains a NUL byte")

        saw_diff = False
        for line in text.splitlines():
            if line in ("GIT binary patch",) or line.startswith("Binary files "):
                raise ValidationError("binary patches are not allowed")
            if line.startswith(cls._UNSAFE_PATCH_PREFIXES):
                raise ValidationError("rename, copy, or mode-change patches are not allowed")
            if line.startswith("new file mode ") or line.startswith("deleted file mode "):
                if line.rsplit(" ", 1)[-1] in {"120000", "160000"}:
                    raise ValidationError("symlink and submodule patches are not allowed")
            if line.startswith("index "):
                fields = line.split()
                if len(fields) >= 3 and fields[-1] in {"120000", "160000"}:
                    raise ValidationError("symlink and submodule patches are not allowed")
            if line.startswith("diff --git "):
                saw_diff = True
                left, right = cls._diff_paths(line)
                cls._validate_path(left)
                cls._validate_path(right)
            elif line.startswith("--- ") or line.startswith("+++ "):
                cls._validate_path(cls._header_path(line[4:]), allow_dev_null=True)

        if not saw_diff:
            raise ValidationError("candidate patch is malformed")

    @staticmethod
    def _diff_paths(line: str) -> tuple[str, str]:
        payload = line[len("diff --git "):]
        try:
            fields = shlex.split(line)
        except ValueError as error:
            raise ValidationError("candidate patch path header is malformed") from error
        if len(fields) == 4 and fields[:2] == ["diff", "--git"]:
            return fields[2], fields[3]
        if payload.startswith("a/") and " b/" in payload:
            left, right = payload.rsplit(" b/", 1)
            return left, "b/" + right
        raise ValidationError("candidate patch path header is malformed")

    @staticmethod
    def _header_path(payload: str) -> str:
        raw = payload.split("\t", 1)[0]
        if raw.startswith('"'):
            try:
                fields = shlex.split(raw)
            except ValueError as error:
                raise ValidationError("candidate patch path header is malformed") from error
            if len(fields) != 1:
                raise ValidationError("candidate patch path header is malformed")
            return fields[0]
        return raw

    @staticmethod
    def _validate_path(value: str, allow_dev_null: bool = False) -> None:
        if allow_dev_null and value == "/dev/null":
            return
        path = value[2:] if value.startswith(("a/", "b/")) else value
        if not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
            raise ValidationError("candidate patch path is unsafe")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts) or any(part.casefold() == ".git" for part in parts):
            raise ValidationError("candidate patch path is unsafe")
        if len(parts[0]) >= 2 and parts[0][1] == ":":
            raise ValidationError("candidate patch path is unsafe")

    def _apply_patch(self, destination: Path, patch: bytes) -> None:
        try:
            subprocess.run(
                ["git", "apply", "--index", "--whitespace=nowarn", "-"],
                cwd=destination,
                input=patch,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValidationError("candidate patch cannot be applied") from error

    def _validate_index(self, destination: Path) -> None:
        try:
            result = subprocess.run(
                ["git", "ls-files", "--stage", "-z"],
                cwd=destination,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValidationError("candidate index cannot be inspected") from error
        for entry in result.stdout.split(b"\x00"):
            if not entry:
                continue
            try:
                metadata, encoded_path = entry.split(b"\t", 1)
                mode = metadata.split(b" ", 1)[0].decode("ascii")
                path = encoded_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise ValidationError("candidate index entry is malformed") from error
            if mode not in {"100644", "100755"}:
                raise ValidationError("candidate index contains a symlink or submodule")
            self._validate_path(path)

    @staticmethod
    def _deterministic_git_environment() -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Meta Loop",
            "GIT_AUTHOR_EMAIL": "meta-loop@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "Meta Loop",
            "GIT_COMMITTER_EMAIL": "meta-loop@example.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    @staticmethod
    def _git(cwd: Path, *arguments: str, input_data: bytes | None = None, environment: dict[str, str] | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                input=input_data,
                check=True,
                capture_output=True,
                env=environment,
            )
            return result.stdout.decode("ascii").strip()
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
            raise ValidationError("candidate Git operation failed") from error
