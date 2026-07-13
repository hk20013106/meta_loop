from datetime import UTC, datetime

import pytest

from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import InvalidTransitionError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def make_task(*, risk: RiskClass = RiskClass.R1) -> Task:
    return Task.create(
        task_id="task-1",
        source=TaskSource(kind="manual", reference="request-1"),
        repository=RepositoryTarget(name="public/repository", revision="main"),
        risk=RiskAssessment(proposed=risk, validated=risk, effective=risk),
        created_at=NOW,
    )


def test_valid_task_creation_has_received_status_and_version_zero():
    task = make_task()

    assert task.status is TaskStatus.RECEIVED
    assert task.version == 0


def test_task_rejects_missing_source_reference():
    with pytest.raises(ValidationError):
        TaskSource(kind="manual", reference="")


def test_task_rejects_invalid_enum_value():
    with pytest.raises(ValidationError):
        RiskAssessment(proposed="R9", validated=RiskClass.R1, effective=RiskClass.R1)


def test_legal_transition_requires_the_authorized_role():
    task = make_task().transition(TaskStatus.VALIDATED, Role.VALIDATOR, NOW)

    assert task.status is TaskStatus.VALIDATED
    assert task.version == 1


def test_illegal_transition_fails_closed():
    with pytest.raises(InvalidTransitionError):
        make_task().transition(TaskStatus.IMPLEMENTING, Role.IMPLEMENTER, NOW)


def test_r3_cannot_enter_implementing_and_becomes_blocked_without_governance():
    task = make_task(risk=RiskClass.R3)
    for status, role in ((TaskStatus.VALIDATED, Role.VALIDATOR), (TaskStatus.PLANNED, Role.PLANNER)):
        task = task.transition(status, role, NOW)
    task = task.transition(TaskStatus.DESIGN_REVIEW, Role.DESIGN_REVIEWER, NOW)

    proposal = task.transition(TaskStatus.PROPOSAL_READY, Role.DESIGN_REVIEWER, NOW)
    blocked = task.transition(TaskStatus.IMPLEMENTING, Role.IMPLEMENTER, NOW)

    assert proposal.status is TaskStatus.PROPOSAL_READY
    assert blocked.status is TaskStatus.BLOCKED


def test_artifact_digest_is_sha256_and_reference_is_immutable():
    digest = ArtifactDigest("a" * 64)
    reference = ArtifactRef(digest=digest, logical_name="plan.json", media_type="application/json")

    assert reference.to_dict()["digest"] == f"sha256:{digest.value}"


def test_review_rework_is_limited_to_three_rounds_then_blocks():
    task = make_task()

    for expected_round in (1, 2, 3):
        task = task.request_rework(Role.DESIGN_REVIEWER)
        assert task.rework_round == expected_round
        if expected_round < 3:
            assert task.status is TaskStatus.REWORK_REQUIRED
            task = task.restart_rework(Role.PLANNER)

    assert task.status is TaskStatus.BLOCKED


def test_only_validator_can_explicitly_unblock_a_task():
    blocked = make_task().block()

    with pytest.raises(InvalidTransitionError):
        blocked.unblock(Role.PLANNER)

    assert blocked.unblock(Role.VALIDATOR).status is TaskStatus.VALIDATED


def test_terminal_task_cannot_transition_or_be_blocked_again():
    failed = make_task().fail(Role.SYSTEM)

    with pytest.raises(InvalidTransitionError):
        failed.transition(TaskStatus.VALIDATED, Role.VALIDATOR, NOW)
    with pytest.raises(InvalidTransitionError):
        failed.block()
