"""GitHub REST read adapter; callers inject transport and keep credentials outside DTOs."""

from datetime import datetime
import json
import os
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

from meta_loop.application.ingestion import IssueCandidate, parse_issue_task_spec
from meta_loop.domain.errors import ValidationError


MAX_RESPONSE_BYTES = 1_048_576


class GitHubIssueSource:
    def __init__(self, repository: str, trigger_author: str, trigger_label: str, transport) -> None:
        if not isinstance(repository, str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None or not self._safe_config(trigger_author) or not self._safe_config(trigger_label):
            raise ValidationError("GitHub Issue source configuration is incomplete")
        self._repository = repository
        self._author = trigger_author.casefold()
        self._label = trigger_label
        self._transport = transport

    def scan(self, limit: int) -> tuple[IssueCandidate, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("GitHub Issue scan limit is invalid")
        path = f"/repos/{self._repository}/issues?state=open&labels={quote(self._label, safe='')}&per_page={limit}"
        candidates = []
        issues = self._list(path, limit)
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state") != "open":
                continue
            number, issue_id = issue.get("number"), issue.get("id")
            if isinstance(number, bool) or not isinstance(number, int) or isinstance(issue_id, bool) or not isinstance(issue_id, int):
                raise ValidationError("GitHub Issue payload is invalid")
            listed_body = issue.get("body")
            if not isinstance(listed_body, str):
                raise ValidationError("GitHub Issue body is invalid")
            listed_spec = parse_issue_task_spec(listed_body)
            event = self._trigger_event(number)
            current = self._transport.get_json(f"/repos/{self._repository}/issues/{number}")
            if not isinstance(current, dict) or isinstance(current.get("id"), bool) or not isinstance(current.get("id"), int) or isinstance(current.get("number"), bool) or not isinstance(current.get("number"), int):
                raise ValidationError("GitHub Issue payload is invalid")
            if current["id"] != issue_id or current["number"] != number or "pull_request" in current or current.get("state") != "open" or not self._has_label(current):
                raise ValidationError("GitHub Issue is no longer an open configured Issue")
            body = current.get("body")
            if not isinstance(body, str):
                raise ValidationError("GitHub Issue body is invalid")
            spec = parse_issue_task_spec(body)
            if spec.canonical() != listed_spec.canonical():
                raise ValidationError("GitHub Issue body or revision drifted during ingestion")
            commit = self._transport.get_json(f"/repos/{self._repository}/commits/{spec.revision}")
            if not isinstance(commit, dict) or commit.get("sha") != spec.revision:
                raise ValidationError("GitHub Issue revision is not a verified commit")
            candidates.append(IssueCandidate("github", f"github:{self._repository}:{issue_id}", f"{self._repository}#{number}", str(event["id"]), event["actor"]["login"], self._label, self._parse_time(event["created_at"]), spec))
        return tuple(candidates)

    def _trigger_event(self, number: int) -> dict:
        events = []
        for page in range(1, 11):
            values = self._list(f"/repos/{self._repository}/issues/{number}/events?per_page=100&page={page}", 100)
            events.extend(values)
            if len(values) < 100:
                break
        else:
            raise ValidationError("GitHub Issue event history exceeds the configured bound")
        matches = [event for event in events if isinstance(event, dict) and event.get("event") == "labeled" and isinstance(event.get("label"), dict) and event["label"].get("name") == self._label]
        if not matches:
            raise ValidationError("GitHub Issue has no matching trigger label event")
        try:
            if any(type(item.get("id")) is not int or not isinstance(item.get("created_at"), str) for item in matches):
                raise ValidationError("GitHub Issue trigger event is incomplete")
            event = max(matches, key=lambda item: (self._parse_time(item["created_at"]), item["id"]))
        except (KeyError, TypeError):
            raise ValidationError("GitHub Issue trigger event is incomplete")
        actor = event.get("actor")
        if not isinstance(actor, dict) or not isinstance(actor.get("login"), str) or actor["login"].casefold() != self._author:
            raise ValidationError("GitHub Issue trigger actor is not authorized")
        if type(event.get("id")) is not int or not isinstance(event.get("created_at"), str):
            raise ValidationError("GitHub Issue trigger event is incomplete")
        return event

    def _list(self, path: str, maximum: int) -> list:
        value = self._transport.get_json(path)
        if not isinstance(value, list) or len(value) > maximum:
            raise ValidationError("GitHub API list response is invalid")
        return value

    def _has_label(self, issue: dict) -> bool:
        labels = issue.get("labels")
        return isinstance(labels, list) and any(isinstance(label, dict) and label.get("name") == self._label for label in labels)

    @staticmethod
    def _safe_config(value) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        if not isinstance(value, str):
            raise ValidationError("GitHub Issue trigger time is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError("GitHub Issue trigger time is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError("GitHub Issue trigger time is invalid")
        return parsed


class UrllibJsonTransport:
    """Small HTTP boundary that keeps the PAT in request headers only."""

    def __init__(self, token: str, opener=urlopen) -> None:
        if not token:
            raise ValidationError("GitHub token is not configured")
        self._token, self._opener = token, opener

    def get_json(self, path: str):
        if not path.startswith("/") or "://" in path:
            raise ValidationError("GitHub API path is invalid")
        request = Request("https://api.github.com" + path, headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self._token, "User-Agent": "meta-loop"})
        try:
            with self._opener(request, timeout=15) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValidationError("GitHub API response is too large")
                return json.loads(payload.decode("utf-8"))
        except Exception as error:
            if isinstance(error, ValidationError):
                raise
            raise ValidationError("GitHub API request failed") from error


def github_issue_source_from_environment(environment=None) -> GitHubIssueSource:
    env = os.environ if environment is None else environment
    required = {name: env.get(name) for name in ("META_LOOP_GITHUB_ORG", "META_LOOP_GITHUB_REPO", "META_LOOP_GITHUB_PAT", "META_LOOP_TRIGGER_AUTHOR", "META_LOOP_TRIGGER_LABEL")}
    if not all(required.values()):
        raise ValueError("GitHub Issue ingestion is not configured")
    return GitHubIssueSource(required["META_LOOP_GITHUB_ORG"] + "/" + required["META_LOOP_GITHUB_REPO"], required["META_LOOP_TRIGGER_AUTHOR"], required["META_LOOP_TRIGGER_LABEL"], UrllibJsonTransport(required["META_LOOP_GITHUB_PAT"]))


def github_issue_source_configured(environment=None) -> bool:
    env = os.environ if environment is None else environment
    return all(env.get(name) for name in ("META_LOOP_GITHUB_ORG", "META_LOOP_GITHUB_REPO", "META_LOOP_GITHUB_PAT", "META_LOOP_TRIGGER_AUTHOR", "META_LOOP_TRIGGER_LABEL"))
