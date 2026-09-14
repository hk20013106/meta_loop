import base64
from hashlib import sha1, sha256
from pathlib import Path
import subprocess

import pytest

from meta_loop.application.models import PublicationEffectIntent, PublicationIntent
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.github import GitHubPublicationPublisher
from meta_loop.infrastructure.publication import LocalGitCandidatePreparer


def run_git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fixture_candidate(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    run_git("init", cwd=source)
    run_git("config", "user.email", "test@example.invalid", cwd=source)
    run_git("config", "user.name", "Meta Loop Test", cwd=source)
    (source / "README.txt").write_text("before\n", encoding="utf-8")
    run_git("add", "README.txt", cwd=source)
    run_git("commit", "-m", "fixture", cwd=source)
    base = run_git("rev-parse", "HEAD", cwd=source)
    (source / "README.txt").write_text("after\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff", "--no-ext-diff", "--", "README.txt"], cwd=source, check=True, capture_output=True).stdout
    run_git("checkout", "--", "README.txt", cwd=source)
    digest = sha256(patch).hexdigest()
    intent = PublicationIntent(
        "publication-1", "task-1", "result-1", digest, "owner/repository", base,
        1, 0, "correlation-1", "governance-1",
    )
    prepared = LocalGitCandidatePreparer({"owner/repository": source}, tmp_path / "prepare").prepare(intent, patch)
    effect = PublicationEffectIntent("effect-1", intent.publication_id, prepared.head_sha, prepared.deterministic_ref)
    return source, intent, prepared, effect


def git_blob_sha(content: bytes) -> str:
    return sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


class FakeTransport:
    def __init__(self, intent, prepared, *, existing_ref=None, existing_pr=False, wrong_tree=False):
        self.intent = intent
        self.prepared = prepared
        self.existing_ref = existing_ref
        self.existing_pr = existing_pr
        self.wrong_tree = wrong_tree
        self.calls = []

    def get_optional_json(self, path):
        self.calls.append(("GET?", path, None))
        if "/git/ref/heads/meta-loop/" in path:
            if self.existing_ref is None:
                return None
            return {"ref": self.prepared.deterministic_ref, "object": {"type": "commit", "sha": self.existing_ref}}
        raise AssertionError(path)

    def get_json(self, path):
        self.calls.append(("GET", path, None))
        if path.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.intent.base_sha}}
        if "/pulls?" in path:
            if not self.existing_pr:
                return []
            return [{
                "number": 17,
                "state": "open",
                "base": {"sha": self.intent.base_sha, "ref": "main"},
                "head": {"sha": self.prepared.head_sha, "ref": "meta-loop/publication-1"},
            }]
        if f"/git/commits/{self.prepared.head_sha}" in path:
            return {"sha": self.prepared.head_sha, "tree": {"sha": self.prepared.tree_sha}}
        raise AssertionError(path)

    def post_json(self, path, payload):
        self.calls.append(("POST", path, payload))
        if path.endswith("/git/blobs"):
            raw = base64.b64decode(payload["content"])
            return {"sha": git_blob_sha(raw)}
        if path.endswith("/git/trees"):
            return {"sha": "0" * 40 if self.wrong_tree else self.prepared.tree_sha}
        if path.endswith("/git/commits"):
            return {"sha": self.prepared.head_sha, "tree": {"sha": self.prepared.tree_sha}}
        if path.endswith("/git/refs"):
            self.existing_ref = payload["sha"]
            return {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
        if path.endswith("/pulls"):
            self.existing_pr = True
            return {
                "number": 17,
                "state": "open",
                "base": {"sha": self.intent.base_sha, "ref": "main"},
                "head": {"sha": self.prepared.head_sha, "ref": "meta-loop/publication-1"},
            }
        raise AssertionError(path)


def test_github_publisher_is_disabled_by_default(tmp_path: Path):
    source, intent, prepared, effect = fixture_candidate(tmp_path)
    transport = FakeTransport(intent, prepared)
    publisher = GitHubPublicationPublisher({"owner/repository": source}, {"owner/repository": "main"}, transport)
    with pytest.raises(ValidationError, match="disabled"):
        publisher.reconcile(intent, prepared, effect)
    assert transport.calls == []


def test_github_publisher_creates_exact_git_objects_branch_and_pr_with_fake_transport(tmp_path: Path):
    source, intent, prepared, effect = fixture_candidate(tmp_path)
    transport = FakeTransport(intent, prepared)
    publisher = GitHubPublicationPublisher(
        {"owner/repository": source}, {"owner/repository": "main"}, transport, enabled=True,
    )

    receipt = publisher.reconcile(intent, prepared, effect)

    assert receipt.pull_request.pull_request_number == 17
    assert receipt.pull_request.base_sha == intent.base_sha
    assert receipt.pull_request.tree_sha == prepared.tree_sha
    assert receipt.pull_request.head_sha == prepared.head_sha
    ref_posts = [payload for method, path, payload in transport.calls if method == "POST" and path.endswith("/git/refs")]
    assert ref_posts == [{"ref": prepared.deterministic_ref, "sha": prepared.head_sha}]
    pr_posts = [payload for method, path, payload in transport.calls if method == "POST" and path.endswith("/pulls")]
    assert pr_posts and pr_posts[0]["head"] == "meta-loop/publication-1" and pr_posts[0]["base"] == "main"
    assert not any(method in {"PATCH", "DELETE"} for method, _, _ in transport.calls)


def test_github_publisher_recovers_existing_identical_branch_and_pr_without_writes(tmp_path: Path):
    source, intent, prepared, effect = fixture_candidate(tmp_path)
    transport = FakeTransport(intent, prepared, existing_ref=prepared.head_sha, existing_pr=True)
    publisher = GitHubPublicationPublisher(
        {"owner/repository": source}, {"owner/repository": "main"}, transport, enabled=True,
    )

    receipt = publisher.reconcile(intent, prepared, effect)

    assert receipt.pull_request.pull_request_number == 17
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_github_publisher_fails_closed_on_existing_ref_drift(tmp_path: Path):
    source, intent, prepared, effect = fixture_candidate(tmp_path)
    transport = FakeTransport(intent, prepared, existing_ref="f" * 40, existing_pr=True)
    publisher = GitHubPublicationPublisher(
        {"owner/repository": source}, {"owner/repository": "main"}, transport, enabled=True,
    )
    with pytest.raises(ValidationError, match="drift"):
        publisher.reconcile(intent, prepared, effect)
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_github_publisher_never_creates_ref_or_pr_when_remote_tree_does_not_match(tmp_path: Path):
    source, intent, prepared, effect = fixture_candidate(tmp_path)
    transport = FakeTransport(intent, prepared, wrong_tree=True)
    publisher = GitHubPublicationPublisher(
        {"owner/repository": source}, {"owner/repository": "main"}, transport, enabled=True,
    )
    with pytest.raises(ValidationError, match="tree"):
        publisher.reconcile(intent, prepared, effect)
    assert not any(method == "POST" and (path.endswith("/git/refs") or path.endswith("/pulls")) for method, path, _ in transport.calls)
