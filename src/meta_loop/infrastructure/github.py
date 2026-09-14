"""GitHub REST adapters; callers inject transport and keep credentials outside DTOs."""

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from meta_loop.application.ingestion import IssueCandidate, parse_issue_task_spec
from meta_loop.application.models import (
    PreparedHead,
    PublicationEffectIntent,
    PublicationEffectState,
    PublicationIntent,
    PublicationReceipt,
    PullRequestReceipt,
)
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


class GitHubPublicationPublisher:
    """Create or recover one exact publication branch and pull request without mutation shortcuts."""

    _IDENTITY = re.compile(r"^(author|committer) (.+) <([^<>]+)> ([0-9]+) ([+-][0-9]{4})$")

    def __init__(
        self,
        repositories: dict[str, Path],
        base_branches: dict[str, str],
        transport,
        *,
        enabled: bool = False,
    ) -> None:
        self._repositories = {name: Path(path) for name, path in repositories.items()}
        self._base_branches = dict(base_branches)
        self._transport = transport
        self._enabled = enabled

    def reconcile(
        self,
        intent: PublicationIntent,
        prepared: PreparedHead,
        effect: PublicationEffectIntent,
    ) -> PublicationReceipt:
        if not self._enabled:
            raise ValidationError("GitHub publication is disabled")
        repository, base_branch, publication_branch = self._validate_binding(intent, prepared, effect)
        local_root = self._repositories[intent.repository_name]
        self._validate_local_candidate(local_root, intent, prepared)
        api = f"/repos/{repository}"

        base = self._transport.get_json(f"{api}/git/ref/heads/{quote(base_branch, safe='/')}")
        if not self._is_ref(base, f"refs/heads/{base_branch}", intent.base_sha):
            raise ValidationError("GitHub base branch drifted from publication base SHA")

        existing_ref = self._transport.get_optional_json(
            f"{api}/git/ref/heads/{quote(publication_branch, safe='/')}"
        )
        if existing_ref is not None:
            if not self._is_ref(existing_ref, prepared.deterministic_ref, prepared.head_sha):
                raise ValidationError("GitHub publication ref drift detected")
            self._validate_remote_commit(api, prepared)
        else:
            self._create_exact_objects(api, local_root, intent, prepared)
            created_ref = self._transport.post_json(
                f"{api}/git/refs",
                {"ref": prepared.deterministic_ref, "sha": prepared.head_sha},
            )
            if not self._is_ref(created_ref, prepared.deterministic_ref, prepared.head_sha):
                raise ValidationError("GitHub publication ref response does not match exact head")

        pull_request = self._find_open_pull_request(
            api, repository, publication_branch, base_branch, intent, prepared
        )
        if pull_request is None:
            pull_request = self._transport.post_json(
                f"{api}/pulls",
                {
                    "title": f"Meta Loop publication {intent.publication_id}",
                    "head": publication_branch,
                    "base": base_branch,
                    "body": f"Publication: {intent.publication_id}\nHead: {prepared.head_sha}\n",
                },
            )
        number = self._validate_pull_request(pull_request, publication_branch, base_branch, intent, prepared)
        return PublicationReceipt(
            intent.publication_id,
            effect.effect_id,
            PublicationEffectState.RECONCILED,
            PullRequestReceipt(
                intent.publication_id,
                number,
                intent.base_sha,
                prepared.tree_sha,
                prepared.head_sha,
            ),
        )

    def _validate_binding(self, intent, prepared, effect) -> tuple[str, str, str]:
        if not isinstance(intent, PublicationIntent) or not isinstance(prepared, PreparedHead) or not isinstance(effect, PublicationEffectIntent):
            raise ValidationError("GitHub publication input is invalid")
        if intent.repository_name not in self._repositories or intent.repository_name not in self._base_branches:
            raise ValidationError("GitHub publication repository is not configured")
        expected_ref = f"refs/heads/meta-loop/{intent.publication_id}"
        if (prepared.publication_id != intent.publication_id
                or prepared.patch_digest != intent.patch_digest
                or prepared.base_sha != intent.base_sha
                or prepared.deterministic_ref != expected_ref
                or effect.publication_id != intent.publication_id
                or effect.head_sha != prepared.head_sha
                or effect.deterministic_ref != expected_ref):
            raise ValidationError("GitHub publication input does not match exact prepared head")
        base_branch = self._base_branches[intent.repository_name]
        if not self._safe_branch(base_branch):
            raise ValidationError("GitHub publication base branch is invalid")
        return intent.repository_name, base_branch, expected_ref.removeprefix("refs/heads/")

    @staticmethod
    def _safe_branch(value) -> bool:
        return (isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()
                and not value.startswith(("/", ".")) and not value.endswith(("/", ".", ".lock"))
                and ".." not in value and "//" not in value and "@{" not in value
                and not any(character in " ~^:?*[\\" or ord(character) < 32 or ord(character) == 127 for character in value))

    def _validate_local_candidate(self, root: Path, intent: PublicationIntent, prepared: PreparedHead) -> None:
        if not root.is_dir():
            raise ValidationError("GitHub publication local repository is unavailable")
        tree = self._git_text(root, "rev-parse", f"{prepared.head_sha}^{{tree}}")
        parent = self._git_text(root, "rev-parse", f"{prepared.head_sha}^")
        if tree != prepared.tree_sha or parent != intent.base_sha:
            raise ValidationError("GitHub publication local head, tree, or base drifted")

    def _create_exact_objects(self, api: str, root: Path, intent: PublicationIntent, prepared: PreparedHead) -> None:
        base_tree = self._git_text(root, "rev-parse", f"{intent.base_sha}^{{tree}}")
        tree_entries = []
        for status, path in self._changed_paths(root, intent.base_sha, prepared.head_sha):
            if status == "D":
                mode, _ = self._tree_entry(root, intent.base_sha, path)
                tree_entries.append({"path": path, "mode": mode, "type": "blob", "sha": None})
                continue
            if status not in {"A", "M"}:
                raise ValidationError("GitHub publication contains an unsupported Git change")
            mode, blob_sha = self._tree_entry(root, prepared.head_sha, path)
            content = self._git_bytes(root, "cat-file", "blob", blob_sha)
            remote_blob = self._transport.post_json(
                f"{api}/git/blobs",
                {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            if not isinstance(remote_blob, dict) or remote_blob.get("sha") != blob_sha:
                raise ValidationError("GitHub blob SHA does not match exact local blob")
            tree_entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})

        remote_tree = self._transport.post_json(
            f"{api}/git/trees", {"base_tree": base_tree, "tree": tree_entries}
        )
        if not isinstance(remote_tree, dict) or remote_tree.get("sha") != prepared.tree_sha:
            raise ValidationError("GitHub tree SHA does not match exact prepared tree")

        metadata = self._commit_payload(root, prepared.head_sha, prepared.tree_sha, intent.base_sha)
        remote_commit = self._transport.post_json(f"{api}/git/commits", metadata)
        if not isinstance(remote_commit, dict) or remote_commit.get("sha") != prepared.head_sha:
            raise ValidationError("GitHub commit SHA does not match exact prepared head")
        self._validate_remote_commit(api, prepared)

    def _validate_remote_commit(self, api: str, prepared: PreparedHead) -> None:
        commit = self._transport.get_json(f"{api}/git/commits/{prepared.head_sha}")
        tree = None if not isinstance(commit, dict) else commit.get("tree")
        if commit.get("sha") != prepared.head_sha or not isinstance(tree, dict) or tree.get("sha") != prepared.tree_sha:
            raise ValidationError("GitHub remote commit tree does not match exact prepared tree")

    def _find_open_pull_request(self, api, repository, publication_branch, base_branch, intent, prepared):
        owner = repository.split("/", 1)[0]
        query = (
            f"state=open&head={quote(owner + ':' + publication_branch, safe='')}"
            f"&base={quote(base_branch, safe='')}&per_page=100"
        )
        values = self._transport.get_json(f"{api}/pulls?{query}")
        if not isinstance(values, list) or len(values) > 1:
            raise ValidationError("GitHub pull request reconciliation is ambiguous")
        if not values:
            return None
        self._validate_pull_request(values[0], publication_branch, base_branch, intent, prepared)
        return values[0]

    @staticmethod
    def _validate_pull_request(value, publication_branch, base_branch, intent, prepared) -> int:
        if not isinstance(value, dict) or value.get("state") != "open":
            raise ValidationError("GitHub pull request is not an open exact publication")
        number = value.get("number")
        base, head = value.get("base"), value.get("head")
        if (type(number) is not int or number <= 0 or not isinstance(base, dict) or not isinstance(head, dict)
                or base.get("sha") != intent.base_sha or base.get("ref") != base_branch
                or head.get("sha") != prepared.head_sha or head.get("ref") != publication_branch):
            raise ValidationError("GitHub pull request drift detected")
        return number

    @staticmethod
    def _is_ref(value, expected_ref: str, expected_sha: str) -> bool:
        obj = None if not isinstance(value, dict) else value.get("object")
        return (isinstance(value, dict) and value.get("ref") == expected_ref and isinstance(obj, dict)
                and obj.get("type") == "commit" and obj.get("sha") == expected_sha)

    @classmethod
    def _commit_payload(cls, root: Path, head_sha: str, tree_sha: str, parent_sha: str) -> dict:
        raw = cls._git_text(root, "cat-file", "commit", head_sha, strip=False)
        headers, separator, message = raw.partition("\n\n")
        if not separator:
            raise ValidationError("GitHub publication commit object is malformed")
        author = committer = None
        tree = parent = None
        for line in headers.splitlines():
            if line.startswith("tree "):
                tree = line[5:]
            elif line.startswith("parent "):
                if parent is not None:
                    raise ValidationError("GitHub publication merge commits are not supported")
                parent = line[7:]
            elif line.startswith("author ") or line.startswith("committer "):
                match = cls._IDENTITY.fullmatch(line)
                if match is None:
                    raise ValidationError("GitHub publication commit identity is malformed")
                identity = {"name": match.group(2), "email": match.group(3), "date": cls._git_date(match.group(4), match.group(5))}
                if match.group(1) == "author":
                    author = identity
                else:
                    committer = identity
        if tree != tree_sha or parent != parent_sha or author is None or committer is None:
            raise ValidationError("GitHub publication commit metadata does not match exact prepared head")
        return {"message": message, "tree": tree_sha, "parents": [parent_sha], "author": author, "committer": committer}

    @staticmethod
    def _git_date(timestamp: str, offset: str) -> str:
        sign = 1 if offset[0] == "+" else -1
        minutes = sign * (int(offset[1:3]) * 60 + int(offset[3:5]))
        zone = timezone(timedelta(minutes=minutes))
        return datetime.fromtimestamp(int(timestamp), tz=zone).isoformat()

    @classmethod
    def _changed_paths(cls, root: Path, base_sha: str, head_sha: str) -> tuple[tuple[str, str], ...]:
        raw = cls._git_bytes(root, "diff", "--name-status", "-z", base_sha, head_sha)
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise ValidationError("GitHub publication diff is malformed")
        changed = []
        for index in range(0, len(fields), 2):
            try:
                status = fields[index].decode("ascii")
                path = fields[index + 1].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError("GitHub publication path is not valid UTF-8") from error
            changed.append((status, path))
        return tuple(changed)

    @classmethod
    def _tree_entry(cls, root: Path, revision: str, path: str) -> tuple[str, str]:
        raw = cls._git_bytes(root, "ls-tree", "-z", revision, "--", path)
        records = [record for record in raw.split(b"\0") if record]
        if len(records) != 1:
            raise ValidationError("GitHub publication tree entry is missing or ambiguous")
        try:
            metadata, encoded_path = records[0].split(b"\t", 1)
            mode, object_type, sha = metadata.decode("ascii").split()
            decoded_path = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationError("GitHub publication tree entry is malformed") from error
        if decoded_path != path or object_type != "blob" or mode not in {"100644", "100755"} or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValidationError("GitHub publication tree entry is unsupported")
        return mode, sha

    @staticmethod
    def _git_bytes(root: Path, *arguments: str) -> bytes:
        try:
            return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValidationError("GitHub publication local Git operation failed") from error

    @classmethod
    def _git_text(cls, root: Path, *arguments: str, strip: bool = True) -> str:
        try:
            value = cls._git_bytes(root, *arguments).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("GitHub publication local Git output is invalid") from error
        return value.strip() if strip else value


class UrllibJsonTransport:
    """Small HTTP boundary that keeps the PAT in request headers only."""

    def __init__(self, token: str, opener=urlopen) -> None:
        if not token:
            raise ValidationError("GitHub token is not configured")
        self._token, self._opener = token, opener

    def get_json(self, path: str):
        return self._request_json(path)

    def get_optional_json(self, path: str):
        return self._request_json(path, optional_not_found=True)

    def post_json(self, path: str, payload):
        return self._request_json(path, method="POST", payload=payload)

    def _request_json(self, path: str, *, method: str = "GET", payload=None, optional_not_found: bool = False):
        if not path.startswith("/") or "://" in path:
            raise ValidationError("GitHub API path is invalid")
        if method not in {"GET", "POST"}:
            raise ValidationError("GitHub API method is invalid")
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self._token, "User-Agent": "meta-loop"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request("https://api.github.com" + path, data=body, headers=headers, method=method)
        try:
            with self._opener(request, timeout=15) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValidationError("GitHub API response is too large")
                return json.loads(data.decode("utf-8"))
        except HTTPError as error:
            if optional_not_found and error.code == 404:
                return None
            raise ValidationError("GitHub API request failed") from error
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
