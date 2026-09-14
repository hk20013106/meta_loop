"""Fail-closed deterministic local preparation and verification for Phase 8."""

from hashlib import sha256
import os
from pathlib import Path
import re
import shlex
import subprocess

from meta_loop.application.models import (
    PreparedHead,
    PublicationIntent,
    VerificationResult,
    VerificationResultCode,
    VerificationStatus,
    VerifierProfile,
)
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
                f"refs/heads/meta-loop/{intent.publication_id}",
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


class DockerCandidateVerifier:
    """Verify an exact prepared head in a locked-down disposable Docker container."""

    _PINNED_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$")

    def __init__(
        self,
        repositories: dict[str, Path],
        managed_root: Path,
        image: str,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int = 300,
        user: str = "65534:65534",
        memory: str = "256m",
        cpus: str = "1.0",
        pids_limit: int = 64,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        if not isinstance(image, str) or self._PINNED_IMAGE.fullmatch(image) is None:
            raise ValidationError("verifier image must be pinned by sha256 digest")
        if not isinstance(argv, tuple) or not argv or any(not isinstance(value, str) or not value or "\x00" in value for value in argv):
            raise ValidationError("verifier argv must be a fixed non-empty tuple")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
            raise ValidationError("verifier timeout is invalid")
        if user in {"0", "0:0", "root", "root:root"}:
            raise ValidationError("verifier must run as non-root")
        if not isinstance(pids_limit, int) or not 1 <= pids_limit <= 4096:
            raise ValidationError("verifier PID limit is invalid")
        self._worktrees = LocalGitWorkspaceManager(repositories, managed_root, forbidden_roots)
        self._image = image
        self._image_digest = image.split("@", 1)[1]
        self._argv = argv
        self._timeout_seconds = timeout_seconds
        self._user = user
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit

    def verify(self, intent: PublicationIntent, prepared: PreparedHead) -> VerificationResult:
        self._validate_binding(intent, prepared)
        allocation_id = f"verify-{prepared.publication_id}"
        try:
            try:
                destination = self._worktrees.checkout_owned(
                    intent.repository_name, allocation_id, prepared.head_sha, read_only=True
                )
            except ValidationError as error:
                raise ValidationError("verification head is not available") from error

            self._validate_checkout(destination, prepared)
            args = self._docker_arguments(destination)
            try:
                completed = subprocess.run(
                    args,
                    timeout=self._timeout_seconds,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._docker_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return self._result(prepared, VerificationResultCode.TIMEOUT)
            except OSError:
                return self._result(prepared, VerificationResultCode.BLOCKED)
            return self._result(
                prepared,
                VerificationResultCode.PASSED if completed.returncode == 0 else VerificationResultCode.FAILED,
            )
        finally:
            self._worktrees.release_owned(intent.repository_name, allocation_id)

    @staticmethod
    def _validate_binding(intent: PublicationIntent, prepared: PreparedHead) -> None:
        if (prepared.publication_id != intent.publication_id
                or prepared.patch_digest != intent.patch_digest
                or prepared.base_sha != intent.base_sha
                or prepared.deterministic_ref != f"refs/heads/meta-loop/{intent.publication_id}"):
            raise ValidationError("verification head does not match publication intent")

    @staticmethod
    def _validate_checkout(destination: Path, prepared: PreparedHead) -> None:
        head = LocalGitCandidatePreparer._git(destination, "rev-parse", "HEAD")
        tree = LocalGitCandidatePreparer._git(destination, "rev-parse", "HEAD^{tree}")
        parent = LocalGitCandidatePreparer._git(destination, "rev-parse", "HEAD^")
        if head != prepared.head_sha or tree != prepared.tree_sha or parent != prepared.base_sha:
            raise ValidationError("verification head, tree, or base does not match prepared candidate")

    def _docker_arguments(self, destination: Path) -> list[str]:
        return [
            "docker", "run",
            "--rm",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self._user,
            "--pids-limit", str(self._pids_limit),
            "--memory", self._memory,
            "--cpus", self._cpus,
            "--mount", f"type=bind,src={destination},dst=/workspace,readonly",
            "--workdir", "/workspace",
            self._image,
            *self._argv,
        ]

    @staticmethod
    def _docker_environment() -> dict[str, str]:
        environment = {"PATH": os.environ.get("PATH", os.defpath)}
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    def _result(self, prepared: PreparedHead, code: VerificationResultCode) -> VerificationResult:
        return VerificationResult(
            prepared.publication_id,
            prepared.head_sha,
            self._image_digest,
            VerifierProfile.DEFAULT,
            VerificationStatus.SUCCEEDED if code is VerificationResultCode.PASSED else VerificationStatus.FAILED,
            code,
        )
