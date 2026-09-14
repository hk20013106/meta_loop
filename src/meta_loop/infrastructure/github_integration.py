import re
from urllib.parse import quote

from meta_loop.application.integration_models import IntegrationIntent, MergeReceipt
from meta_loop.domain.errors import ValidationError


_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubIntegrationGateway:
    """Read/reconcile protected squash merge state without direct ref mutation."""

    def __init__(self, transport, repositories: tuple[str, ...], target_branches: dict[str, str] | None = None) -> None:
        if not isinstance(repositories, tuple) or not repositories or len(set(repositories)) != len(repositories):
            raise ValidationError("GitHub integration repository configuration is invalid")
        self._repositories = frozenset(repositories)
        self._target_branches = {name: "main" for name in repositories} if target_branches is None else dict(target_branches)
        if set(self._target_branches) != set(repositories) or any(not self._safe_branch(value) for value in self._target_branches.values()):
            raise ValidationError("GitHub integration target branch configuration is invalid")
        self._transport = transport

    def target_branch(self, repository_name: str) -> str:
        self._validate_repository(repository_name)
        return self._target_branches[repository_name]

    def read_base_tree(self, repository_name: str, base_sha: str) -> str:
        self._validate_repository(repository_name)
        self._validate_sha(base_sha, "base SHA")
        value = self._transport.get_json(f"/repos/{repository_name}/git/commits/{base_sha}")
        tree = None if not isinstance(value, dict) else value.get("tree")
        tree_sha = None if not isinstance(tree, dict) else tree.get("sha")
        if value.get("sha") != base_sha or not isinstance(tree_sha, str) or _SHA.fullmatch(tree_sha) is None:
            raise ValidationError("GitHub base commit tree is invalid")
        return tree_sha

    def reconcile_merge(self, intent: IntegrationIntent) -> MergeReceipt:
        if not isinstance(intent, IntegrationIntent):
            raise ValidationError("GitHub integration intent is invalid")
        self._validate_repository(intent.repository_name)
        configured_branch = self.target_branch(intent.repository_name)
        if intent.target_branch != configured_branch:
            raise ValidationError("GitHub integration target branch does not match configuration")
        api = f"/repos/{intent.repository_name}"
        pull = self._read_pull(api, intent)
        if self._is_merged(pull):
            return self._reconcile_merged(api, intent, pull)

        target = self._transport.get_json(f"{api}/git/ref/heads/{quote(intent.target_branch, safe='/')}")
        if not self._is_ref(target, f"refs/heads/{intent.target_branch}", intent.base_sha):
            raise ValidationError("GitHub target branch drifted from integration base SHA")

        response = self._transport.put_json(
            f"{api}/pulls/{intent.pull_request_number}/merge",
            {"merge_method": "squash", "sha": intent.approved_head_sha},
        )
        if not isinstance(response, dict) or response.get("merged") is not True:
            raise ValidationError("GitHub protected squash merge was not accepted")
        pull = self._read_pull(api, intent)
        if not self._is_merged(pull):
            raise ValidationError("GitHub merge response was not reconcilable")
        return self._reconcile_merged(api, intent, pull)

    def _read_pull(self, api: str, intent: IntegrationIntent) -> dict:
        value = self._transport.get_json(f"{api}/pulls/{intent.pull_request_number}")
        if not isinstance(value, dict) or value.get("number") != intent.pull_request_number:
            raise ValidationError("GitHub pull request identity is invalid")
        base, head = value.get("base"), value.get("head")
        expected_branch = intent.deterministic_ref.removeprefix("refs/heads/")
        if (not isinstance(base, dict) or not isinstance(head, dict)
                or base.get("sha") != intent.base_sha or base.get("ref") != intent.target_branch
                or head.get("sha") != intent.approved_head_sha or head.get("ref") != expected_branch):
            raise ValidationError("GitHub pull request base or head drift detected")
        if not self._is_merged(value) and value.get("state") != "open":
            raise ValidationError("GitHub pull request is not open or merged")
        return value

    def _reconcile_merged(self, api: str, intent: IntegrationIntent, pull: dict) -> MergeReceipt:
        merge_sha = pull.get("merge_commit_sha")
        self._validate_sha(merge_sha, "merge SHA")
        target = self._transport.get_json(f"{api}/git/ref/heads/{quote(intent.target_branch, safe='/')}")
        if not self._is_ref(target, f"refs/heads/{intent.target_branch}", merge_sha):
            raise ValidationError("GitHub target branch does not point to exact merge SHA")
        commit = self._transport.get_json(f"{api}/git/commits/{merge_sha}")
        tree = None if not isinstance(commit, dict) else commit.get("tree")
        parents = None if not isinstance(commit, dict) else commit.get("parents")
        if (commit.get("sha") != merge_sha
                or not isinstance(tree, dict) or tree.get("sha") != intent.prepared_tree_sha
                or not isinstance(parents, list) or len(parents) != 1
                or not isinstance(parents[0], dict) or parents[0].get("sha") != intent.base_sha):
            raise ValidationError("GitHub merge parent or tree drift detected")
        return MergeReceipt(
            intent.integration_id,
            intent.pull_request_number,
            intent.base_sha,
            intent.approved_head_sha,
            merge_sha,
            intent.prepared_tree_sha,
            intent.base_sha,
            intent.target_branch,
        )

    def _validate_repository(self, repository_name: str) -> None:
        if repository_name not in self._repositories:
            raise ValidationError("GitHub integration repository is not configured")

    @staticmethod
    def _validate_sha(value, label: str) -> None:
        if not isinstance(value, str) or _SHA.fullmatch(value) is None:
            raise ValidationError(f"GitHub {label} is invalid")

    @staticmethod
    def _is_merged(value: dict) -> bool:
        return isinstance(value, dict) and value.get("merged") is True and value.get("state") == "closed" and bool(value.get("merged_at"))

    @staticmethod
    def _is_ref(value, expected_ref: str, expected_sha: str) -> bool:
        obj = None if not isinstance(value, dict) else value.get("object")
        return isinstance(value, dict) and value.get("ref") == expected_ref and isinstance(obj, dict) and obj.get("type") == "commit" and obj.get("sha") == expected_sha

    @staticmethod
    def _safe_branch(value) -> bool:
        return (isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()
                and not value.startswith(("/", ".")) and not value.endswith(("/", ".", ".lock"))
                and ".." not in value and "//" not in value and "@{" not in value
                and not any(character in " ~^:?*[\\" or ord(character) < 32 or ord(character) == 127 for character in value))
