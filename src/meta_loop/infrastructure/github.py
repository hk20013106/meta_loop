"""GitHub REST read adapter; callers inject transport and keep credentials outside DTOs."""

from datetime import datetime
import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from meta_loop.application.ingestion import IssueCandidate, parse_issue_task_spec
from meta_loop.domain.errors import ValidationError


class GitHubIssueSource:
    def __init__(self, repository: str, trigger_author: str, trigger_label: str, transport) -> None:
        if not repository or "/" not in repository or not trigger_author or not trigger_label:
            raise ValidationError("GitHub Issue source configuration is incomplete")
        self._repository = repository
        self._author = trigger_author.casefold()
        self._label = trigger_label
        self._transport = transport

    def scan(self, limit: int) -> tuple[IssueCandidate, ...]:
        if not 1 <= limit <= 100:
            raise ValidationError("GitHub Issue scan limit is invalid")
        path = f"/repos/{self._repository}/issues?state=open&labels={quote(self._label, safe='')}&per_page={limit}"
        candidates = []
        for issue in self._transport.get_json(path):
            if "pull_request" in issue or issue.get("state") != "open":
                continue
            number, issue_id = issue.get("number"), issue.get("id")
            if not isinstance(number, int) or issue_id is None:
                raise ValidationError("GitHub Issue payload is invalid")
            event = self._trigger_event(number)
            spec = parse_issue_task_spec(str(issue.get("body") or ""))
            commit = self._transport.get_json(f"/repos/{self._repository}/commits/{spec.revision}")
            if commit.get("sha", "").lower() != spec.revision.lower():
                raise ValidationError("GitHub Issue revision is not a verified commit")
            candidates.append(IssueCandidate("github", f"github:{self._repository}:{issue_id}", f"{self._repository}#{number}", str(event["id"]), str(event["actor"]["login"]), self._label, self._parse_time(str(event["created_at"])), spec))
        return tuple(candidates)

    def _trigger_event(self, number: int) -> dict:
        events = self._transport.get_json(f"/repos/{self._repository}/issues/{number}/events")
        matches = [event for event in events if event.get("event") == "labeled" and event.get("label", {}).get("name") == self._label]
        if not matches:
            raise ValidationError("GitHub Issue has no matching trigger label event")
        event = matches[-1]
        if str(event.get("actor", {}).get("login", "")).casefold() != self._author:
            raise ValidationError("GitHub Issue trigger actor is not authorized")
        if not event.get("id") or not event.get("created_at"):
            raise ValidationError("GitHub Issue trigger event is incomplete")
        return event

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError("GitHub Issue trigger time is invalid") from error


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
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ValidationError("GitHub API request failed") from error
