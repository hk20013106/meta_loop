"""Argparse-only local CLI; no external command execution."""

import argparse
from contextlib import redirect_stderr
from io import StringIO
import json
import sys
from pathlib import Path

from meta_loop.application.controller import FuseService, IntakeRequest, TaskIntakeService, TaskQueryService
from meta_loop.cli.runtime import (
    UnavailableGovernance,
    check_publication,
    doctor,
    github_ingestion_service,
    github_issue_source,
    prepare_publication,
    publication_status,
    publish_publication,
    unit_of_work,
)
from meta_loop.domain.enums import RiskClass, TaskStatus
from meta_loop.domain.errors import DomainError, IdempotencyConflictError, TaskNotFoundError
from meta_loop.infrastructure.memory import UUIDIdGenerator
from datetime import datetime, timezone


class _ParseError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _ParseError()


def _emit(value: dict, as_json: bool, command: str) -> None:
    if as_json:
        print(json.dumps({"command": command, "ok": True, "result": value, "schema_version": 1}, sort_keys=True, default=str))
    else:
        print("\n".join(f"{key}: {value[key]}" for key in sorted(value)))


def _error(as_json: bool, command: str, error: Exception, details: dict | None = None) -> int:
    if isinstance(error, IdempotencyConflictError):
        code = "idempotency_conflict"
    elif isinstance(error, (OSError, ValueError, KeyError, DomainError, TaskNotFoundError)):
        code = "rejected"
    else:
        code = "operation_failed"
    if as_json:
        payload = {"code": code, "message": "operation was not completed"}
        if details:
            payload.update(details)
        print(json.dumps({"command": command, "error": payload, "ok": False, "schema_version": 1}, sort_keys=True))
    else:
        print("meta-loop: operation was not completed", file=sys.stderr)
    return 2 if code != "operation_failed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="meta-loop")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor"); doctor_parser.add_argument("--json", action="store_true")
    start = sub.add_parser("start"); start.add_argument("--request", required=True); start.add_argument("--enqueue", action="store_true"); start.add_argument("--priority", type=int, default=0); start.add_argument("--json", action="store_true")
    status = sub.add_parser("status"); status.add_argument("task_id"); status.add_argument("--json", action="store_true")
    tasks = sub.add_parser("tasks"); tasks.add_argument("--status"); tasks.add_argument("--risk", choices=[item.value for item in RiskClass]); tasks.add_argument("--json", action="store_true")
    fuse = sub.add_parser("fuse"); fuse.add_argument("action", choices=("status", "engage", "release")); fuse.add_argument("--governance-revision"); fuse.add_argument("--json", action="store_true")

    publication = sub.add_parser("publication")
    publication_sub = publication.add_subparsers(dest="publication_action", required=True)
    prepare = publication_sub.add_parser("prepare")
    prepare.add_argument("--publication-id", required=True)
    prepare.add_argument("--task-id", required=True)
    prepare.add_argument("--worker-result-id", required=True)
    prepare.add_argument("--patch-digest", required=True)
    prepare.add_argument("--json", action="store_true")
    publication_status_parser = publication_sub.add_parser("status")
    publication_status_parser.add_argument("--publication-id", required=True)
    publication_status_parser.add_argument("--json", action="store_true")

    github = sub.add_parser("github")
    github_sub = github.add_subparsers(dest="github_action", required=True)
    sync = github_sub.add_parser("sync"); sync.add_argument("--limit", type=int, default=50); sync.add_argument("--json", action="store_true")
    publish = github_sub.add_parser("publish"); publish.add_argument("--publication-id", required=True); publish.add_argument("--json", action="store_true")
    checks = github_sub.add_parser("checks"); checks.add_argument("--publication-id", required=True); checks.add_argument("--json", action="store_true")
    return parser


def request_from_json(path: str, enqueue: bool, priority: int) -> IntakeRequest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported request schema_version")
    return IntakeRequest(data["request_id"], data["source"]["kind"], data["source"]["reference"], data["repository"]["name"], data["repository"]["revision"], RiskClass(data["risk"]["proposed"]), RiskClass(data["risk"]["validated"]), RiskClass(data["risk"]["effective"]), data["risk"].get("governance_revision"), enqueue, priority)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _task_summary(task, events=()) -> dict:
    return {"task_id": task.task_id, "status": task.status.value, "risk": task.risk.effective.value,
            "version": task.version, "created_at": task.created_at.isoformat(),
            "latest_event": None if not events else {"event_id": events[-1].event_id, "type": events[-1].event_type.value, "occurred_at": events[-1].occurred_at.isoformat()}}


def _prepared_summary(prepared) -> dict:
    return {
        "publication_id": prepared.publication_id,
        "patch_digest": prepared.patch_digest,
        "base_sha": prepared.base_sha,
        "tree_sha": prepared.tree_sha,
        "head_sha": prepared.head_sha,
        "deterministic_ref": prepared.deterministic_ref,
    }


def _receipt_summary(receipt) -> dict:
    pull_request = receipt.pull_request
    if pull_request is None:
        raise ValueError("publication receipt has no pull request")
    return {
        "publication_id": receipt.publication_id,
        "effect_id": receipt.effect_id,
        "pull_request_number": pull_request.pull_request_number,
        "base_sha": pull_request.base_sha,
        "tree_sha": pull_request.tree_sha,
        "head_sha": pull_request.head_sha,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in arguments
    try:
        if json_mode:
            with redirect_stderr(StringIO()):
                args = build_parser().parse_args(arguments)
        else:
            args = build_parser().parse_args(arguments)
    except _ParseError:
        return _error(json_mode, _raw_command(arguments), ValueError("invalid command arguments"))
    except SystemExit:
        if not json_mode:
            raise
        command = _raw_command(arguments)
        return _error(True, command, ValueError("invalid command arguments"))
    try:
        if args.command == "doctor":
            _emit({"status": "ok", **doctor()}, args.json, "doctor")
            return 0
        if args.command == "start":
            request = request_from_json(args.request, args.enqueue, args.priority)
            with unit_of_work() as uow:
                task = TaskIntakeService(SystemClock(), UUIDIdGenerator()).start(uow, request)
                uow.commit()
            _emit(_task_summary(task), args.json, "start")
            return 0
        if args.command == "status":
            with unit_of_work() as uow:
                task, events = TaskQueryService().status(uow, args.task_id)
            _emit(_task_summary(task, events), args.json, "status")
            return 0
        if args.command == "tasks":
            status = TaskStatus(args.status) if args.status else None
            risk = RiskClass(args.risk) if args.risk else None
            with unit_of_work() as uow:
                values = TaskQueryService().tasks(uow, status, risk)
            _emit({"tasks": [_task_summary(task) for task in values]}, args.json, "tasks")
            return 0
        if args.command == "publication":
            if args.publication_action == "prepare":
                prepared = prepare_publication(args.publication_id, args.task_id, args.worker_result_id, args.patch_digest)
                _emit(_prepared_summary(prepared), args.json, "publication.prepare")
                return 0
            value = publication_status(args.publication_id)
            _emit(value, args.json, "publication.status")
            return 0
        if args.command == "github":
            if args.github_action == "publish":
                receipt = publish_publication(args.publication_id)
                _emit(_receipt_summary(receipt), args.json, "github.publish")
                return 0
            if args.github_action == "checks":
                result = check_publication(args.publication_id)
                _emit({
                    "publication_id": result.publication_id,
                    "head_sha": result.head_sha,
                    "required_checks": list(result.required_checks),
                    "passed": result.passed,
                }, args.json, "github.checks")
                return 0
            try:
                service, outcomes = github_ingestion_service(), []
                for candidate in github_issue_source().scan(args.limit):
                    try:
                        with unit_of_work() as uow:
                            receipt = service.ingest(uow, candidate)
                            uow.commit()
                        outcomes.append({"source_key": candidate.source_key, "task_id": receipt.task_id, "status": "ingested" if receipt.created else "duplicate"})
                    except IdempotencyConflictError:
                        outcomes.append({"source_key": candidate.source_key, "status": "conflict"})
                if any(value["status"] == "conflict" for value in outcomes):
                    return _error(args.json, "github.sync", IdempotencyConflictError("GitHub sync has source conflicts"), {"results": outcomes})
                _emit({"results": outcomes}, args.json, "github.sync")
                return 0
            except Exception as error:
                return _error(args.json, "github.sync", error)
        with unit_of_work() as uow:
            service, clock = FuseService(), SystemClock()
            if args.action == "status":
                state = uow.fuse.get()
            elif args.action == "engage":
                state = service.engage(uow, clock.now())
                uow.commit()
            else:
                state = service.release(uow, clock.now(), UnavailableGovernance(), args.governance_revision)
                uow.commit()
            _emit({"engaged": state.engaged, "changed_at": state.changed_at.isoformat(), "governance_revision": state.governance_revision}, args.json, f"fuse.{args.action}")
        return 0
    except (OSError, ValueError, KeyError, DomainError, TaskNotFoundError) as error:
        return _error(args.json, _command_from_args(args), error)
    except Exception as error:
        return _error(args.json, _command_from_args(args), error)


def _command_from_args(args) -> str:
    if args.command == "github":
        return f"github.{args.github_action}"
    if args.command == "publication":
        return f"publication.{args.publication_action}"
    return args.command


def _raw_command(arguments: list[str]) -> str:
    for index, value in enumerate(arguments):
        if value == "github":
            if index + 1 < len(arguments) and arguments[index + 1] in {"sync", "publish", "checks"}:
                return f"github.{arguments[index + 1]}"
            return "github"
        if value == "publication":
            if index + 1 < len(arguments) and arguments[index + 1] in {"prepare", "status"}:
                return f"publication.{arguments[index + 1]}"
            return "publication"
        if value in {"doctor", "start", "status", "tasks", "fuse"}:
            return value
    return "unknown"
