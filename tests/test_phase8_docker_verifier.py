from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from meta_loop.application.models import PreparedHead, PublicationIntent, VerificationResultCode, VerificationStatus
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.publication import DockerCandidateVerifier, LocalGitCandidatePreparer


ALPINE_IMAGE = "alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b"


def run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def fixture_candidate(tmp_path: Path) -> tuple[Path, PublicationIntent, PreparedHead]:
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
    digest = sha256(patch).hexdigest()
    intent = PublicationIntent(
        "publication-1", "task-1", "result-1", digest, "fixture/repo", base_sha,
        1, 0, "correlation-1", "governance-1",
    )
    prepared = LocalGitCandidatePreparer({"fixture/repo": source}, tmp_path / "prepare").prepare(intent, patch)
    return source, intent, prepared


def test_docker_verifier_runs_exact_head_in_locked_down_disposable_container(tmp_path: Path):
    source, intent, prepared = fixture_candidate(tmp_path)
    verifier = DockerCandidateVerifier(
        {"fixture/repo": source},
        tmp_path / "verify",
        ALPINE_IMAGE,
        ("/bin/sh", "-c", "test \"$(cat /workspace/README.txt)\" = after"),
        timeout_seconds=60,
    )

    result = verifier.verify(intent, prepared)

    assert result.publication_id == prepared.publication_id
    assert result.head_sha == prepared.head_sha
    assert result.verifier_image_digest == ALPINE_IMAGE.split("@", 1)[1]
    assert result.status is VerificationStatus.SUCCEEDED
    assert result.result_code is VerificationResultCode.PASSED
    assert run_git("status", "--porcelain", cwd=source) == ""
    assert not (tmp_path / "verify").exists() or not any((tmp_path / "verify").iterdir())


def test_docker_verifier_rejects_unpinned_image_and_head_drift_before_docker(tmp_path: Path, monkeypatch):
    source, intent, prepared = fixture_candidate(tmp_path)
    with pytest.raises(ValidationError, match="pinned"):
        DockerCandidateVerifier({"fixture/repo": source}, tmp_path / "verify", "alpine:3.22", ("true",))

    verifier = DockerCandidateVerifier({"fixture/repo": source}, tmp_path / "verify", ALPINE_IMAGE, ("true",))
    drifted = PreparedHead(
        prepared.publication_id, prepared.patch_digest, prepared.base_sha, prepared.tree_sha,
        "f" * 40, prepared.deterministic_ref,
    )
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(ValidationError, match="head"):
        verifier.verify(intent, drifted)
    assert calls == []


def test_docker_verifier_uses_required_security_flags_and_returns_controlled_failure(tmp_path: Path, monkeypatch):
    source, intent, prepared = fixture_candidate(tmp_path)
    verifier = DockerCandidateVerifier(
        {"fixture/repo": source}, tmp_path / "verify", ALPINE_IMAGE, ("verify-fixed",), timeout_seconds=7,
    )
    real_run = subprocess.run
    docker_calls = []

    def fake_run(args, *pargs, **kwargs):
        if args and args[0] == "docker":
            docker_calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 9, stdout=b"unsafe output", stderr=b"secret-looking output")
        return real_run(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = verifier.verify(intent, prepared)

    assert result.status is VerificationStatus.FAILED
    assert result.result_code is VerificationResultCode.FAILED
    assert len(docker_calls) == 1
    args, kwargs = docker_calls[0]
    joined = " ".join(args)
    for required in ("--rm", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit", "--memory", "--cpus", "--user"):
        assert required in joined
    assert "readonly" in joined
    assert kwargs["timeout"] == 7
    assert kwargs["capture_output"] is True
    assert "META_LOOP_TEST_POSTGRES_DSN" not in kwargs.get("env", {})


def test_docker_verifier_maps_timeout_without_leaking_output(tmp_path: Path, monkeypatch):
    source, intent, prepared = fixture_candidate(tmp_path)
    verifier = DockerCandidateVerifier({"fixture/repo": source}, tmp_path / "verify", ALPINE_IMAGE, ("true",), timeout_seconds=1)
    real_run = subprocess.run

    def fake_run(args, *pargs, **kwargs):
        if args and args[0] == "docker":
            raise subprocess.TimeoutExpired(args, 1, output=b"do-not-return", stderr=b"do-not-return")
        return real_run(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = verifier.verify(intent, prepared)
    assert result.status is VerificationStatus.FAILED
    assert result.result_code is VerificationResultCode.TIMEOUT
    assert "do-not-return" not in result.canonical()
