"""Argparse-only local CLI; no external command execution."""

import argparse
from contextlib import redirect_stderr
from io import StringIO
import json
import sys
from pathlib import Path

from meta_loop.application.controller import FuseService, IntakeRequest, TaskIntakeService, TaskQueryService
from meta_loop.cli.runtime import UnavailableGovernance, doctor, github_ingestion_service, github_issue_source, unit_of_work
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
    doctor = sub.add_parser("doctor"); doctor.add_argument("--json", action="store_true")
    start = sub.add_parser("start"); start.add_argument("--request", required=True); start.add_argument("--enqueue", action="store_true"); start.add_argument("--priority", type=int, default=0); start.add_argument("--json", action="store_true")
    status = sub.add_parser("status"); status.add_argument("task_id"); status.add_argument("--json", action="store_true")
    tasks = sub.add_parser("tasks"); tasks.add_argument("--status"); tasks.add_argument("--risk", choices=[item.value for item in RiskClass]); tasks.add_argument("--json", action="store_true")
    fuse = sub.add_parser("fuse"); fuse.add_argument("action", choices=("status", "engage", "release")); fuse.add_argument("--governance-revision"); fuse.add_argument("--json", action="store_true")
    github = sub.add_parser("github"); github_sub = github.add_subparsers(dest="github_action", required=True); sync = github_sub.add_parser("sync"); sync.add_argument("--limit", type=int, default=50); sync.add_argument("--json", action="store_true")
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
        if args.command == "github":
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
        return _error(args.json, args.command, error)
    except Exception as error:
        return _error(args.json, args.command, error)


def _raw_command(arguments: list[str]) -> str:
    for index, value in enumerate(arguments):
        if value == "github":
            return "github.sync" if index + 1 < len(arguments) and arguments[index + 1] == "sync" else "github"
        if value in {"doctor", "start", "status", "tasks", "fuse"}:
            return value
    return "unknown"
