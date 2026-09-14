import pytest

from meta_loop.application.integration_models import CanaryResult, CanaryStatus, IntegrationIntent, IntegrationRecord, IntegrationState, MergeReceipt, RevertCandidate
from meta_loop.domain.errors import ValidationError

BASE = "a" * 40
BASE_TREE = "b" * 40
HEAD = "c" * 40
TREE = "d" * 40
MERGE = "e" * 40


def _intent(**changes):
    values = {
        "integration_id": "integration-1",
        "publication_id": "publication-1",
        "effect_id": "effect-1",
        "task_id": "task-1",
        "repository_name": "owner/repository",
        "target_branch": "main",
        "pull_request_number": 17,
        "base_sha": BASE,
        "base_tree_sha": BASE_TREE,
        "approved_head_sha": HEAD,
        "prepared_tree_sha": TREE,
        "deterministic_ref": "refs/heads/meta-loop/publication-1",
        "governance_revision": "gov-1",
        "expected_task_version": 3,
        "expected_sequence": 4,
    }
    values.update(changes)
    return IntegrationIntent(**values)


def test_intent_validates_exact_identity():
    value = _intent()
    assert value.to_dict()["approved_head_sha"] == HEAD
    assert value.to_dict()["base_tree_sha"] == BASE_TREE
    assert value.canonical() == value.canonical()
    with pytest.raises(ValidationError):
        _intent(approved_head_sha="c" * 39)
    with pytest.raises(ValidationError):
        _intent(deterministic_ref="refs/heads/other/publication-1")


def test_merge_canary_and_revert_bind_exact_shas():
    receipt = MergeReceipt("integration-1", 17, BASE, HEAD, MERGE, TREE, BASE, "main")
    assert CanaryResult("integration-1", MERGE, CanaryStatus.PASSED).sha == MERGE
    with pytest.raises(ValidationError):
        MergeReceipt("integration-1", 17, BASE, HEAD, MERGE, TREE, "f" * 40, "main")
    candidate = RevertCandidate("integration-1", MERGE, BASE_TREE, "f" * 40, "refs/heads/meta-loop/revert/integration-1")
    assert candidate.parent_sha == MERGE


def test_record_starts_at_merge_requested():
    record = IntegrationRecord(_intent())
    assert record.state is IntegrationState.MERGE_REQUESTED
    assert record.version == 0
