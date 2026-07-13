"""Argparse-only local CLI; no external command execution."""

import argparse
import json
import sys
from pathlib import Path

from meta_loop.application.controller import FuseService, IntakeRequest, TaskIntakeService, TaskQueryService
from meta_loop.cli.runtime import UnavailableGovernance, doctor, unit_of_work
from meta_loop.domain.enums import RiskClass, TaskStatus
from meta_loop.domain.errors import DomainError, TaskNotFoundError
from meta_loop.infrastructure.memory import SequentialIdGenerator
from datetime import datetime, timezone


def _emit(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"schema_version": 1, **value}, sort_keys=True, default=str))
    else:
        print("\n".join(f"{key}: {value[key]}" for key in sorted(value)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meta-loop")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); doctor.add_argument("--json", action="store_true")
    start = sub.add_parser("start"); start.add_argument("--request", required=True); start.add_argument("--enqueue", action="store_true"); start.add_argument("--priority", type=int, default=0); start.add_argument("--json", action="store_true")
    status = sub.add_parser("status"); status.add_argument("task_id"); status.add_argument("--json", action="store_true")
    tasks = sub.add_parser("tasks"); tasks.add_argument("--status"); tasks.add_argument("--risk", choices=[item.value for item in RiskClass]); tasks.add_argument("--json", action="store_true")
    fuse = sub.add_parser("fuse"); fuse.add_argument("action", choices=("status", "engage", "release")); fuse.add_argument("--governance-revision"); fuse.add_argument("--json", action="store_true")
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
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        _emit({"status": "ok", **doctor()}, args.json)
        return 0
    try:
        if args.command == "start":
            request = request_from_json(args.request, args.enqueue, args.priority)
            with unit_of_work() as uow:
                task = TaskIntakeService(SystemClock(), SequentialIdGenerator("event")).start(uow, request)
                uow.commit()
            _emit(_task_summary(task), args.json)
            return 0
        if args.command == "status":
            with unit_of_work() as uow:
                task, events = TaskQueryService().status(uow, args.task_id)
            _emit(_task_summary(task, events), args.json)
            return 0
        if args.command == "tasks":
            status = TaskStatus(args.status) if args.status else None
            risk = RiskClass(args.risk) if args.risk else None
            with unit_of_work() as uow:
                values = TaskQueryService().tasks(uow, status, risk)
            _emit({"tasks": [_task_summary(task) for task in values]}, args.json)
            return 0
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
            _emit({"engaged": state.engaged, "changed_at": state.changed_at.isoformat(), "governance_revision": state.governance_revision}, args.json)
        return 0
    except (OSError, ValueError, KeyError, DomainError, TaskNotFoundError) as error:
        print(f"meta-loop: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("meta-loop: operation failed", file=sys.stderr)
        return 1
