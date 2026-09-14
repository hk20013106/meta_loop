"""Shared Phase 8 adapter contract: identical semantics across memory and PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from meta_loop.application import models
from meta_loop.application.artifacts import ArtifactRequest
from meta_loop.domain.artifacts import ArtifactDigest, ArtifactRef
from meta_loop.domain.enums import RiskClass, Role, TaskStatus
from meta_loop.domain.errors import IdempotencyConflictError, ValidationError
from meta_loop.domain.task import RepositoryTarget, RiskAssessment, Task, TaskSource
from meta_loop.infrastructure.memory import InMemoryUnitOfWork

NOW = datetime(2026, 7, 18, tzinfo=UTC)
POSTGRES_DSN = os.environ.get("META_LOOP_TEST_POSTGRES_DSN")
PATCH = ArtifactRef(ArtifactDigest("b" * 64), "change.diff", "text/x-diff")
REVIEW = ArtifactRef(ArtifactDigest("a" * 64), "review.json", "application/json")


def _intent(**changes) -> models.PublicationIntent:
    values = {
        "publication_id": "publication-1",
        "task_id": "task-1",
        "worker_result_id": "result-1",
        "patch_digest": "b" * 64,
        "repository_name": "owner/repository",
        "base_sha": "c" * 40,
        "expected_task_version": 1,
        "expected_sequence": 0,
        "correlation_id": "correlation-1",
        "governance_revision": "governance-1",
    }
    values.update(changes)
    return models.PublicationIntent(**values)


def _prepared() -> models.PreparedHead:
    return models.PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, "refs/meta-loop/publication-1")


def _verification() -> models.VerificationResult:
    return models.VerificationResult(
        "publication-1", "e" * 40, "sha256:" + "f" * 64, models.VerifierProfile.DEFAULT,
        models.VerificationStatus.SUCCEEDED, models.VerificationResultCode.PASSED,
    )


def _approval(**changes) -> models.PublicationReviewDecision:
    values = {
        "approval_id": "approval-1",
        "publication_id": "publication-1",
        "task_id": "task-1",
        "session_id": "review-session",
        "result_id": "review-result",
        "approval_artifact": REVIEW,
        "patch_digest": "b" * 64,
        "base_sha": "c" * 40,
        "tree_sha": "d" * 40,
        "head_sha": "e" * 40,
        "reviewer_id": "reviewer-1",
        "decision": models.ReviewDecision.APPROVED,
        "decided_at": NOW,
    }
    values.update(changes)
    return models.PublicationReviewDecision(**values)


def _observation(**changes) -> models.CheckRunObservation:
    values = {
        "publication_id": "publication-1",
        "head_sha": "e" * 40,
        "check_name": "lint",
        "status": models.CheckRunStatus.COMPLETED,
        "conclusion": models.CheckRunConclusion.SUCCESS,
        "observed_at": NOW,
    }
    values.update(changes)
    return models.CheckRunObservation(**values)


def _seed(uow) -> None:
    initial = Task.create("task-1", TaskSource("synthetic", "fixture"), RepositoryTarget("owner/repository", "c" * 40), RiskAssessment(RiskClass.R1, RiskClass.R1, RiskClass.R1), NOW)
    task = Task("task-1", initial.source, initial.repository, initial.risk, NOW, TaskStatus.READY_TO_PUBLISH, 1, 0, (PATCH,))
    uow.tasks.create(initial)
    uow.tasks.update(task, 0)
    uow.artifacts.register(models.CatalogedArtifact(PATCH, "internal", "worker_result:result-1", 10))
    uow.artifacts.register(models.CatalogedArtifact(REVIEW, "internal", "review", 10))
    uow.runner_sessions.record_once(models.RunnerSessionRequest("session-1", "task-1", Role.IMPLEMENTER, 1, 0, (), 60, "implementer-correlation"))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("session-1", models.RunnerSessionState.SUCCEEDED, "ready"))
    uow.runner_sessions.record_once(models.RunnerSessionRequest("review-session", "task-1", Role.PATCH_REVIEWER, 0, 0, (), 60, "review-correlation"))
    uow.runner_sessions.set_outcome(models.RunnerSessionOutcome("review-session", models.RunnerSessionState.SUCCEEDED, "approved"))
    uow.worker_results.record_once(models.WorkerResult("result-1", "session-1", "task-1", models.WorkerIdentity("worker-1", Role.IMPLEMENTER), TaskStatus.READY_TO_PUBLISH, "ready", 1, 0))
    uow.worker_results.record_once(models.WorkerResult("review-result", "review-session", "task-1", models.WorkerIdentity("reviewer-1", Role.PATCH_REVIEWER), TaskStatus.READY_TO_PUBLISH, "approved", 0, 0))


def _seed_verified(uow) -> None:
    _seed(uow)
    uow.publications.reserve(_intent())
    uow.publications.record_prepared(_prepared(), 0)
    uow.publications.record_verification(_verification(), 1)


def _seed_approved(uow) -> None:
    _seed_verified(uow)
    uow.publication_approvals.append(_approval())


def _postgres_connection():
    psycopg = pytest.importorskip("psycopg")
    return psycopg.connect(POSTGRES_DSN)


@pytest.fixture(scope="module")
def postgres_schema():
    if POSTGRES_DSN is None:
        pytest.skip("BLOCKED_ENVIRONMENT: META_LOOP_TEST_POSTGRES_DSN is not configured")
    from meta_loop.infrastructure.migrations import MigrationRunner

    with _postgres_connection() as connection:
        MigrationRunner(Path("migrations")).apply(connection)
        connection.commit()
    return True


def _truncate() -> None:
    with _postgres_connection() as connection, connection.cursor() as cursor:
        cursor.execute("TRUNCATE check_run_observations, publication_effects, publication_approvals, publications, source_ingestions, workspace_allocations, worker_results, runner_sessions, artifact_catalog, task_events, task_queue, tasks CASCADE")
        connection.commit()


@pytest.fixture(params=("memory", "postgres"))
def uow_factory(request):
    if request.param == "memory":
        return InMemoryUnitOfWork
    if POSTGRES_DSN is None:
        pytest.skip("BLOCKED_ENVIRONMENT: META_LOOP_TEST_POSTGRES_DSN is not configured")
    request.getfixturevalue("postgres_schema")
    _truncate()
    from meta_loop.infrastructure.postgres import PostgresUnitOfWork

    return lambda: PostgresUnitOfWork(_postgres_connection)


@pytest.mark.parametrize("repository_name", ("/repo", "owner/", "owner//repo", "owner/../repo", "owner/repo\nname", "owner/repo name", "o" * 64 + "/" + "r" * 70))
def test_publication_intent_rejects_malformed_repository_names(repository_name):
    with pytest.raises(ValidationError):
        _intent(repository_name=repository_name)


@pytest.mark.parametrize("logical_name", ("/var/tmp/review.json", "C:\\temp\\review.json", "../review.json", "review\n.json", "review\x00.json", "review;curl", "-leading-dash.json", "postgresql://db.example.invalid/publications", "password=example-value", "token:example-value", "", "n" * 129))
def test_artifact_logical_name_boundary_rejects_unsafe_values(logical_name):
    with pytest.raises(ValidationError):
        ArtifactRef(ArtifactDigest("a" * 64), logical_name, "application/json")
    with pytest.raises(ValidationError):
        ArtifactRequest(logical_name, "application/json", "internal", "review")


def test_reserve_rejects_stale_expected_sequence(uow_factory):
    with uow_factory() as uow:
        _seed(uow)
        with pytest.raises(ValidationError):
            uow.publications.reserve(_intent(expected_sequence=7))


def test_reserve_is_idempotent_and_conflicts_are_controlled(uow_factory):
    with uow_factory() as uow:
        _seed(uow)
        assert uow.publications.reserve(_intent())[1] is True
        assert uow.publications.reserve(_intent())[1] is False
        with pytest.raises(IdempotencyConflictError):
            uow.publications.reserve(_intent(correlation_id="different-correlation"))


def test_approval_append_is_idempotent_and_conflicts_are_controlled(uow_factory):
    with uow_factory() as uow:
        _seed_verified(uow)
        assert uow.publication_approvals.append(_approval())[1] is True
        assert uow.publication_approvals.append(_approval())[1] is False
        with pytest.raises(IdempotencyConflictError):
            uow.publication_approvals.append(_approval(head_sha="f" * 40))


def test_check_observation_append_is_idempotent_and_conflicts_are_controlled(uow_factory):
    with uow_factory() as uow:
        _seed_verified(uow)
        assert uow.check_observations.append(_observation())[1] is True
        assert uow.check_observations.append(_observation())[1] is False
        with pytest.raises(IdempotencyConflictError):
            uow.check_observations.append(_observation(conclusion=models.CheckRunConclusion.FAILURE))


def test_effect_request_is_idempotent_and_conflicts_are_controlled(uow_factory):
    intent = models.PublicationEffectIntent("effect-1", "publication-1", "e" * 40, "refs/meta-loop/publication-1")
    with uow_factory() as uow:
        _seed_approved(uow)
        assert uow.publication_effects.request(intent)[1] is True
        assert uow.publication_effects.request(intent)[1] is False
        with pytest.raises(IdempotencyConflictError):
            uow.publication_effects.request(models.PublicationEffectIntent("effect-2", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))


def _race(operation):
    """Run operation twice on distinct connections, released together by a barrier."""
    barrier = Barrier(2)

    def invoke(index):
        barrier.wait()
        try:
            return operation(index), None
        except BaseException as error:  # noqa: BLE001 - the raised type is the assertion subject
            return None, error

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(invoke, (0, 1)))


def _committed(call):
    from meta_loop.infrastructure.postgres import PostgresUnitOfWork

    def operation(index):
        with PostgresUnitOfWork(_postgres_connection) as uow:
            value = call(uow, index)
            uow.commit()
            return value

    return operation


def _assert_no_database_error_escaped(results) -> None:
    psycopg = pytest.importorskip("psycopg")
    for _, error in results:
        assert not isinstance(error, psycopg.Error), f"raw database error escaped the port contract: {type(error)}"


def _assert_one_created_one_existing(results) -> None:
    _assert_no_database_error_escaped(results)
    assert [error for _, error in results if error is not None] == []
    assert sorted(value for value, _ in results) == [False, True]


def _assert_exactly_one_controlled_conflict(results) -> None:
    _assert_no_database_error_escaped(results)
    errors = [error for _, error in results if error is not None]
    assert len(errors) == 1
    assert type(errors[0]) is IdempotencyConflictError
    assert [value for value, error in results if error is None] == [True]


@pytest.mark.parametrize("conflicting", (False, True))
def test_concurrent_reserve_never_leaks_a_database_error(postgres_schema, conflicting):
    _truncate()
    _committed(lambda uow, _index: _seed(uow))(0)

    def call(uow, index):
        return uow.publications.reserve(_intent(correlation_id="different-correlation" if conflicting and index else "correlation-1"))[1]

    results = _race(_committed(call))
    (_assert_exactly_one_controlled_conflict if conflicting else _assert_one_created_one_existing)(results)


@pytest.mark.parametrize("conflicting", (False, True))
def test_concurrent_approval_append_never_leaks_a_database_error(postgres_schema, conflicting):
    _truncate()
    _committed(lambda uow, _index: _seed_verified(uow))(0)

    def call(uow, index):
        return uow.publication_approvals.append(_approval(reviewer_id="different-reviewer" if conflicting and index else "reviewer-1"))[1]

    results = _race(_committed(call))
    (_assert_exactly_one_controlled_conflict if conflicting else _assert_one_created_one_existing)(results)


@pytest.mark.parametrize("conflicting", (False, True))
def test_concurrent_check_observation_never_leaks_a_database_error(postgres_schema, conflicting):
    _truncate()
    _committed(lambda uow, _index: _seed_verified(uow))(0)

    def call(uow, index):
        return uow.check_observations.append(_observation(conclusion=models.CheckRunConclusion.FAILURE if conflicting and index else models.CheckRunConclusion.SUCCESS))[1]

    results = _race(_committed(call))
    (_assert_exactly_one_controlled_conflict if conflicting else _assert_one_created_one_existing)(results)


def test_concurrent_same_effect_id_never_leaks_a_database_error(postgres_schema):
    _truncate()
    _committed(lambda uow, _index: _seed_approved(uow))(0)

    def call(uow, _index):
        return uow.publication_effects.request(models.PublicationEffectIntent("effect-1", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))[1]

    _assert_one_created_one_existing(_race(_committed(call)))


def test_concurrent_distinct_effect_ids_for_one_ref_never_leak_a_database_error(postgres_schema):
    _truncate()
    _committed(lambda uow, _index: _seed_approved(uow))(0)

    def call(uow, index):
        return uow.publication_effects.request(models.PublicationEffectIntent(f"effect-{index + 1}", "publication-1", "e" * 40, "refs/meta-loop/publication-1"))[1]

    _assert_exactly_one_controlled_conflict(_race(_committed(call)))
