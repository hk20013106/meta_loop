import pytest

from meta_loop.application.integration_models import IntegrationIntent
from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.github_integration import GitHubIntegrationGateway

BASE = "a" * 40
BASE_TREE = "b" * 40
HEAD = "c" * 40
TREE = "d" * 40
MERGE = "e" * 40


def intent():
    return IntegrationIntent(
        "integration-1", "publication-1", "effect-1", "task-1", "owner/repository",
        "main", 17, BASE, BASE_TREE, HEAD, TREE,
        "refs/heads/meta-loop/publication-1", "gov-1", 1, 0,
    )


class Transport:
    def __init__(self, *, merged=False, target_sha=None, merge_tree=TREE):
        self.merged = merged
        self.target_sha = target_sha or (MERGE if merged else BASE)
        self.merge_tree = merge_tree
        self.put_calls = []
        self.get_calls = []

    def get_json(self, path):
        self.get_calls.append(path)
        if path.endswith("/git/commits/" + BASE):
            return {"sha": BASE, "tree": {"sha": BASE_TREE}, "parents": [{"sha": "9" * 40}]}
        if path.endswith("/pulls/17"):
            return {
                "number": 17,
                "state": "closed" if self.merged else "open",
                "merged": self.merged,
                "merged_at": "2026-09-15T00:00:00Z" if self.merged else None,
                "merge_commit_sha": MERGE if self.merged else None,
                "head": {"sha": HEAD, "ref": "meta-loop/publication-1"},
                "base": {"sha": BASE, "ref": "main"},
            }
        if path.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.target_sha}}
        if path.endswith("/git/commits/" + MERGE):
            return {"sha": MERGE, "tree": {"sha": self.merge_tree}, "parents": [{"sha": BASE}]}
        raise AssertionError(path)

    def put_json(self, path, payload):
        self.put_calls.append((path, payload))
        assert payload["merge_method"] == "squash"
        assert payload["sha"] == HEAD
        self.merged = True
        self.target_sha = MERGE
        return {"merged": True, "sha": MERGE, "message": "merged"}


def test_gateway_reads_base_tree_and_merges_exact_open_pr():
    transport = Transport()
    gateway = GitHubIntegrationGateway(transport, ("owner/repository",))
    assert gateway.read_base_tree("owner/repository", BASE) == BASE_TREE
    receipt = gateway.reconcile_merge(intent())
    assert receipt.merge_sha == MERGE
    assert receipt.merge_tree_sha == TREE
    assert len(transport.put_calls) == 1
    assert transport.put_calls[0][1]["sha"] == HEAD


def test_gateway_reconciles_already_merged_pr_without_second_write():
    transport = Transport(merged=True)
    receipt = GitHubIntegrationGateway(transport, ("owner/repository",)).reconcile_merge(intent())
    assert receipt.merge_sha == MERGE
    assert transport.put_calls == []


def test_gateway_rejects_target_or_merge_tree_drift():
    with pytest.raises(ValidationError, match="target"):
        GitHubIntegrationGateway(Transport(target_sha="f" * 40), ("owner/repository",)).reconcile_merge(intent())
    with pytest.raises(ValidationError, match="tree"):
        GitHubIntegrationGateway(Transport(merged=True, merge_tree="f" * 40), ("owner/repository",)).reconcile_merge(intent())
