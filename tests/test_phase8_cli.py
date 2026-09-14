import json
from types import SimpleNamespace

from meta_loop.cli.main import main


BASE = "c" * 40
TREE = "d" * 40
HEAD = "e" * 40


def test_publication_prepare_and_status_json_use_safe_stable_envelopes(monkeypatch, capsys):
    monkeypatch.setattr(
        "meta_loop.cli.main.prepare_publication",
        lambda publication_id, task_id, worker_result_id, patch_digest: SimpleNamespace(
            publication_id=publication_id,
            patch_digest=patch_digest,
            base_sha=BASE,
            tree_sha=TREE,
            head_sha=HEAD,
            deterministic_ref=f"refs/heads/meta-loop/{publication_id}",
        ),
        raising=False,
    )
    assert main([
        "publication", "prepare",
        "--publication-id", "publication-1",
        "--task-id", "task-1",
        "--worker-result-id", "result-1",
        "--patch-digest", "b" * 64,
        "--json",
    ]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["command"] == "publication.prepare"
    assert prepared["ok"] is True
    assert prepared["result"] == {
        "publication_id": "publication-1",
        "patch_digest": "b" * 64,
        "base_sha": BASE,
        "tree_sha": TREE,
        "head_sha": HEAD,
        "deterministic_ref": "refs/heads/meta-loop/publication-1",
    }

    monkeypatch.setattr(
        "meta_loop.cli.main.publication_status",
        lambda publication_id: {
            "publication_id": publication_id,
            "state": "verified",
            "version": 2,
            "head_sha": HEAD,
            "tree_sha": TREE,
        },
        raising=False,
    )
    assert main(["publication", "status", "--publication-id", "publication-1", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["command"] == "publication.status"
    assert status["result"]["state"] == "verified"


def test_github_publish_and_checks_json_use_normalized_receipts(monkeypatch, capsys):
    monkeypatch.setattr(
        "meta_loop.cli.main.publish_publication",
        lambda publication_id: SimpleNamespace(
            publication_id=publication_id,
            effect_id=f"publish:{publication_id}",
            pull_request=SimpleNamespace(
                pull_request_number=17,
                base_sha=BASE,
                tree_sha=TREE,
                head_sha=HEAD,
            ),
        ),
        raising=False,
    )
    assert main(["github", "publish", "--publication-id", "publication-1", "--json"]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["command"] == "github.publish"
    assert published["result"] == {
        "publication_id": "publication-1",
        "effect_id": "publish:publication-1",
        "pull_request_number": 17,
        "base_sha": BASE,
        "tree_sha": TREE,
        "head_sha": HEAD,
    }

    monkeypatch.setattr(
        "meta_loop.cli.main.check_publication",
        lambda publication_id: SimpleNamespace(
            publication_id=publication_id,
            head_sha=HEAD,
            required_checks=("test",),
            passed=True,
        ),
        raising=False,
    )
    assert main(["github", "checks", "--publication-id", "publication-1", "--json"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["command"] == "github.checks"
    assert checked["result"] == {
        "publication_id": "publication-1",
        "head_sha": HEAD,
        "required_checks": ["test"],
        "passed": True,
    }


def test_phase8_cli_errors_are_redacted_and_there_is_no_approve_command(monkeypatch, capsys):
    def fail(publication_id):
        raise ValueError("github_pat_secret C:\\private\\repo postgresql://secret")

    monkeypatch.setattr("meta_loop.cli.main.publish_publication", fail, raising=False)
    assert main(["github", "publish", "--publication-id", "publication-1", "--json"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value == {
        "command": "github.publish",
        "error": {"code": "rejected", "message": "operation was not completed"},
        "ok": False,
        "schema_version": 1,
    }

    assert main(["publication", "approve", "--publication-id", "publication-1", "--json"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["command"] == "publication"
    assert value["ok"] is False
