from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from meta_loop.application.models import PublicationIntent
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.publication import LocalGitCandidatePreparer


def run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repository(tmp_path: Path) -> tuple[Path, str, bytes]:
    source = tmp_path / "source"
    source.mkdir()
    run_git("init", cwd=source)
    run_git("config", "user.email", "test@example.invalid", cwd=source)
    run_git("config", "user.name", "Meta Loop Test", cwd=source)
    (source / "README.txt").write_text("before\n", encoding="utf-8")
    run_git("add", "README.txt", cwd=source)
    run_git("commit", "-m", "fixture", cwd=source)
    base_sha = run_git("rev-parse", "HEAD", cwd=source)
    (source / "README.txt").write_text("after\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff", "--no-ext-diff", "--", "README.txt"], cwd=source, check=True, capture_output=True).stdout
    run_git("checkout", "--", "README.txt", cwd=source)
    return source, base_sha, patch


def intent(base_sha: str, patch: bytes, **changes) -> PublicationIntent:
    values = {
        "publication_id": "publication-1",
        "task_id": "task-1",
        "worker_result_id": "result-1",
        "patch_digest": sha256(patch).hexdigest(),
        "repository_name": "fixture/repo",
        "base_sha": base_sha,
        "expected_task_version": 1,
        "expected_sequence": 7,
        "correlation_id": "correlation-1",
        "governance_revision": "governance-1",
    }
    values.update(changes)
    return PublicationIntent(**values)


def test_local_candidate_preparer_is_deterministic_and_disposable(tmp_path: Path):
    source, base_sha, patch = make_repository(tmp_path)
    managed = tmp_path / "managed"
    preparer = LocalGitCandidatePreparer({"fixture/repo": source}, managed)

    first = preparer.prepare(intent(base_sha, patch), patch)
    second = preparer.prepare(intent(base_sha, patch), patch)

    assert first == second
    assert first.patch_digest == sha256(patch).hexdigest()
    assert first.base_sha == base_sha
    assert len(first.tree_sha) == 40
    assert len(first.head_sha) == 40
    assert first.deterministic_ref == "refs/heads/meta-loop/publication-1"
    assert run_git("status", "--porcelain", cwd=source) == ""
    assert not managed.exists() or not any(managed.iterdir())


@pytest.mark.parametrize(
    "patch",
    [
        b"diff --git a/../escape.txt b/../escape.txt\n--- a/../escape.txt\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+x\n",
        b"diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n@@ -0,0 +1 @@\n+x\n",
        b"diff --git a/file.bin b/file.bin\nGIT binary patch\nliteral 0\nHcmV?d00001\n",
        b"diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n",
        b"diff --git a/README.txt b/README.txt\nold mode 100644\nnew mode 100755\n",
    ],
)
def test_local_candidate_preparer_rejects_unsafe_patch_forms(tmp_path: Path, patch: bytes):
    source, base_sha, _ = make_repository(tmp_path)
    preparer = LocalGitCandidatePreparer({"fixture/repo": source}, tmp_path / "managed")

    with pytest.raises(ValidationError):
        preparer.prepare(intent(base_sha, patch), patch)


def test_local_candidate_preparer_requires_allowlisted_repo_exact_base_and_digest(tmp_path: Path):
    source, base_sha, patch = make_repository(tmp_path)
    preparer = LocalGitCandidatePreparer({"fixture/repo": source}, tmp_path / "managed")

    with pytest.raises(ValidationError, match="allowed"):
        preparer.prepare(intent(base_sha, patch, repository_name="other/repo"), patch)
    with pytest.raises(ValidationError, match="base"):
        preparer.prepare(intent("0" * 40, patch), patch)
    with pytest.raises(ValidationError, match="digest"):
        preparer.prepare(intent(base_sha, patch, patch_digest="0" * 64), patch)
