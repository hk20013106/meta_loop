import json
from types import SimpleNamespace

from meta_loop.cli.main import main


class _FakeUow:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def commit(self):
        return None


def _github_fakes(monkeypatch, receipts):
    candidates = [SimpleNamespace(source_key=f"source-{index}") for index in range(len(receipts))]
    monkeypatch.setattr("meta_loop.cli.main.github_issue_source", lambda: SimpleNamespace(scan=lambda limit: candidates))
    monkeypatch.setattr("meta_loop.cli.main.unit_of_work", _FakeUow)
    class Service:
        def __init__(self):
            self.receipts = iter(receipts)
        def ingest(self, uow, candidate):
            value = next(self.receipts)
            if isinstance(value, Exception):
                raise value
            return value
    monkeypatch.setattr("meta_loop.cli.main.github_ingestion_service", Service)


def test_github_sync_json_error_is_stable_and_redacts_exception(monkeypatch, capsys):
    def fail():
        raise ValueError("github_pat_secret_should_not_appear")

    monkeypatch.setattr("meta_loop.cli.main.github_ingestion_service", fail)
    assert main(["github", "sync", "--json"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value == {
        "command": "github.sync",
        "error": {"code": "rejected", "message": "operation was not completed"},
        "ok": False,
        "schema_version": 1,
    }


def test_doctor_json_uses_the_common_success_envelope(monkeypatch, capsys):
    monkeypatch.setattr("meta_loop.cli.main.doctor", lambda: {"database": "not configured"})
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": "doctor",
        "ok": True,
        "result": {"database": "not configured", "status": "ok"},
        "schema_version": 1,
    }


def test_documented_json_commands_use_a_redacted_error_envelope(capsys):
    for argv, command in ((["start", "--request", "missing.json", "--json"], "start"), (["status", "missing", "--json"], "status"), (["tasks", "--json"], "tasks"), (["fuse", "status", "--json"], "fuse")):
        assert main(argv) == 2
        value = json.loads(capsys.readouterr().out)
        assert value["command"] == command
        assert value["ok"] is False
        assert set(value) == {"command", "error", "ok", "schema_version"}


def test_invalid_json_mode_invocation_uses_the_common_error_envelope(capsys):
    assert main(["start", "--json"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["command"] == "start"
    assert value["ok"] is False
    assert set(value) == {"command", "error", "ok", "schema_version"}


def test_invalid_github_sync_json_invocation_uses_the_nested_command(capsys):
    assert main(["github", "sync", "--limit", "bad", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["command"] == "github.sync"


def test_plaintext_errors_do_not_echo_paths(monkeypatch, capsys):
    monkeypatch.setattr("meta_loop.cli.main.doctor", lambda: (_ for _ in ()).throw(OSError(r"C:\\private\\dsn")))
    assert main(["doctor"]) == 2
    assert capsys.readouterr().err == "meta-loop: operation was not completed\n"


def test_plaintext_parse_errors_do_not_echo_raw_arguments(capsys):
    assert main(["start", r"--bad=C:\\secret\\github_pat_value"]) == 2
    assert capsys.readouterr().err == "meta-loop: operation was not completed\n"


def test_github_sync_json_success_and_duplicate_results_are_stable(monkeypatch, capsys):
    _github_fakes(monkeypatch, [SimpleNamespace(task_id="task-a", created=True), SimpleNamespace(task_id="task-b", created=False)])
    assert main(["github", "sync", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["results"] == [
        {"source_key": "source-0", "status": "ingested", "task_id": "task-a"},
        {"source_key": "source-1", "status": "duplicate", "task_id": "task-b"},
    ]


def test_github_sync_json_conflict_preserves_per_candidate_order(monkeypatch, capsys):
    from meta_loop.domain.errors import IdempotencyConflictError
    _github_fakes(monkeypatch, [SimpleNamespace(task_id="task-a", created=True), IdempotencyConflictError("conflict"), SimpleNamespace(task_id="task-c", created=False)])
    assert main(["github", "sync", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["results"] == [
        {"source_key": "source-0", "status": "ingested", "task_id": "task-a"},
        {"source_key": "source-1", "status": "conflict"},
        {"source_key": "source-2", "status": "duplicate", "task_id": "task-c"},
    ]
