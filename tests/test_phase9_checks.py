from datetime import UTC, datetime, timedelta

import pytest

from meta_loop.application.checks import RequiredCheckEvaluator, RequiredCheckStatus
from meta_loop.application.models import CheckRunConclusion, CheckRunObservation, CheckRunStatus
from meta_loop.domain.errors import ValidationError

NOW = datetime(2026, 9, 15, tzinfo=UTC)
SHA = "a" * 40


def obs(name, status, conclusion=None, seconds=0, sha=SHA):
    return CheckRunObservation("subject-1", sha, name, status, conclusion, NOW + timedelta(seconds=seconds))


def test_required_checks_distinguish_pending_passed_and_failed():
    evaluator = RequiredCheckEvaluator(("test", "lint"))
    pending = evaluator.evaluate((obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),), SHA)
    assert pending.status is RequiredCheckStatus.PENDING
    passed = evaluator.evaluate((
        obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),
        obs("lint", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),
    ), SHA)
    assert passed.status is RequiredCheckStatus.PASSED
    failed = evaluator.evaluate((
        obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),
        obs("lint", CheckRunStatus.COMPLETED, CheckRunConclusion.FAILURE),
    ), SHA)
    assert failed.status is RequiredCheckStatus.FAILED


def test_pending_beats_terminal_failure_until_every_required_check_is_terminal():
    evaluator = RequiredCheckEvaluator(("test", "lint"))
    result = evaluator.evaluate((
        obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.FAILURE),
        obs("lint", CheckRunStatus.IN_PROGRESS),
    ), SHA)
    assert result.status is RequiredCheckStatus.PENDING


def test_evaluator_uses_latest_snapshot_and_rejects_wrong_sha_or_ambiguity():
    evaluator = RequiredCheckEvaluator(("test",))
    result = evaluator.evaluate((
        obs("test", CheckRunStatus.IN_PROGRESS),
        obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS, seconds=1),
    ), SHA)
    assert result.status is RequiredCheckStatus.PASSED
    with pytest.raises(ValidationError):
        evaluator.evaluate((obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS, sha="b" * 40),), SHA)
    with pytest.raises(ValidationError):
        evaluator.evaluate((
            obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),
            obs("test", CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS),
        ), SHA)
