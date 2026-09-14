# Phase 9 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Phase 9 protected merge -> exact merge-SHA canary -> deterministic protected revert workflow defined by `docs/phases/PHASE9_SPEC.md`.

**Architecture:** Add one `IntegrationLedger` as the sole Phase 9 lifecycle owner. Reuse Phase 8 publication identity, approval, GitHub exact-object PR publication, required-check normalization, and the existing fuse; all external GitHub I/O occurs after durable intent commit and is reconciled in a later transaction.

**Tech Stack:** Python 3.13, PostgreSQL 16, DB-API adapters, GitHub REST transport, argparse CLI, pytest.

**Spec:** `docs/phases/PHASE9_SPEC.md`

## Global Constraints

- SINGLE OWNER: only `IntegrationLedger` owns Phase 9 lifecycle state.
- HASH BYTES NOT MEANING; never accept an equivalent tree/commit in place of the exact recorded SHA.
- NO RE-HASHING DOWNSTREAM.
- Reuse the Phase 8 exact-object PR publisher and normalized required-check evaluator; do not create parallel owners.
- Normal merge requires fuse off; exact failed-integration recovery is the only Phase 9 write allowed while fuse is engaged.
- GitHub network I/O never occurs inside a PostgreSQL transaction.
- Merge and rollback use protected `squash` only; no force, direct target-ref mutation, delete, bypass, auto-merge, Task completion, Issue closure, or self-modification.
- `0001`-`0009` migrations remain immutable; Phase 9 adds only `0010_phase9_integrations.sql`.

---

### Task 1: Integration domain contract and in-memory ledger

**Files:**
- Create: `src/meta_loop/application/integration_models.py`
- Modify: `src/meta_loop/application/ports.py`
- Modify: `src/meta_loop/infrastructure/memory.py`
- Test: `tests/test_phase9_models.py`
- Test: `tests/test_phase9_memory.py`

**Interfaces:**
- Produce `IntegrationState`, `CanaryStatus`, `IntegrationIntent`, `MergeReceipt`, `RevertCandidate`, `IntegrationRecord`.
- Produce `IntegrationLedger.reserve(intent)`, `get(integration_id)`, `record_merge(receipt, expected_version)`, `record_canary(...)`, `record_revert_candidate(...)`, `record_revert_publication(...)`, `record_revert_checks(...)`, `record_revert_merge(...)`, `mark_manual(...)`.

- [ ] Write failing tests proving strict SHA/ref validation, canonical serialization, legal state progression, idempotency conflict rejection, and one unresolved integration per `(repository_name, target_branch)`.
- [ ] Run `python -m pytest -q tests/test_phase9_models.py tests/test_phase9_memory.py` and confirm RED because Phase 9 types/ledger do not exist.
- [ ] Implement the minimal immutable models and memory ledger. State progression must be explicit; callers cannot assign arbitrary states.
- [ ] Re-run the focused tests and require PASS.
- [ ] Commit `feat: add Phase 9 integration ledger contract`.

Core state enum:
```python
class IntegrationState(str, Enum):
    MERGE_REQUESTED = "merge_requested"
    MERGED = "merged"
    CANARY_PENDING = "canary_pending"
    CANARY_PASSED = "canary_passed"
    CANARY_FAILED = "canary_failed"
    REVERT_REQUESTED = "revert_requested"
    REVERT_PUBLISHED = "revert_published"
    REVERT_CHECKED = "revert_checked"
    REVERT_MERGED = "revert_merged"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
```

### Task 2: PostgreSQL `0010` and UoW integration

**Files:**
- Create: `src/meta_loop/infrastructure/sql/0010_phase9_integrations.sql`
- Modify: `src/meta_loop/infrastructure/postgres.py`
- Modify: `src/meta_loop/infrastructure/memory.py`
- Modify: `src/meta_loop/application/ports.py`
- Test: `tests/test_phase9_postgres.py`
- Modify: `tests/test_phase1b_migrations.py`

**Interfaces:** `UnitOfWork.integrations: IntegrationLedger`; PostgreSQL and memory adapters must satisfy the same conflict/idempotency contract.

- [ ] Write RED PostgreSQL tests for migration `0009 -> 0010`, insert/read/update versioning, append-only effect facts, and concurrent reservation of the same target branch.
- [ ] Run focused PostgreSQL tests against `META_LOOP_TEST_POSTGRES_DSN`; confirm failure because `0010`/adapter are absent.
- [ ] Add `0010_phase9_integrations.sql` with immutable integration identity plus unique unresolved-target ownership enforced transactionally.
- [ ] Add PostgreSQL adapter and wire both UoWs without changing older migrations.
- [ ] Run Phase 9 PostgreSQL tests plus existing migration tests; require PASS.
- [ ] Commit `feat: persist Phase 9 integration state`.

### Task 3: Generalize the single required-check evaluator

**Files:**
- Create: `src/meta_loop/application/checks.py`
- Modify: `src/meta_loop/application/publication.py`
- Modify: `src/meta_loop/infrastructure/github_checks.py`
- Test: `tests/test_phase9_checks.py`
- Re-run existing Phase 8 check tests.

**Interfaces:**
```python
RequiredCheckEvaluator.evaluate(required_checks, observations, exact_sha) -> CheckEvaluation
```
`CheckEvaluation` classifies `PENDING`, `PASSED`, or `FAILED`; wrong SHA, malformed duplicates, or ambiguity raises `ValidationError`.

- [ ] Write RED tests showing one evaluator handles PR-head, merge-SHA, and revert-head snapshots and never treats missing/pending checks as failure.
- [ ] Extract normalization/evaluation from `PublicationCheckService`; adapt Phase 8 to call the shared evaluator with no behavior change.
- [ ] Run Phase 8 + Phase 9 check tests; require PASS.
- [ ] Commit `refactor: share exact-SHA required check evaluation`.

### Task 4: Protected merge service and fake-first GitHub merge adapter

**Files:**
- Create: `src/meta_loop/application/integration.py`
- Create: `src/meta_loop/infrastructure/github_integration.py`
- Test: `tests/test_phase9_merge.py`

**Interfaces:**
```python
IntegrationMergeService.merge(integration_id, publication_id, effect_id) -> MergeReceipt
GitHubIntegrationGateway.reconcile_merge(intent) -> MergeReceipt
```

- [ ] Write RED tests for Phase 8 exact identity, fresh PR/check state, Task/version/R0-R2/governance/fuse gates, target branch exactly at base SHA, Meta Loop self-target rejection, and one active integration per target.
- [ ] Write recovery tests: durable `MERGE_REQUESTED` exists before remote I/O; timeout after remote merge reconciles without issuing a second merge.
- [ ] Implement service so transaction 1 validates/reserves/commits, GitHub I/O occurs outside UoW, and transaction 2 reconciles the exact receipt.
- [ ] Implement fake-first gateway: verify open PR/base/head/ref/target; request `squash` with expected head SHA; reconcile merged PR, single parent == base SHA, tree == prepared tree, and target == merge SHA. Drift -> manual state + fuse.
- [ ] Run focused tests and full Phase 8 publication tests; require PASS.
- [ ] Commit `feat: add protected Phase 9 merge recovery`.

### Task 5: Merge-SHA canary and atomic fuse engagement

**Files:**
- Modify: `src/meta_loop/application/integration.py`
- Test: `tests/test_phase9_canary.py`

**Interfaces:** `IntegrationCanaryService.check(integration_id) -> CanaryStatus`.

- [ ] Write RED tests for `PENDING`, `PASSED`, `FAILED`, `DRIFTED`; observations must be for exact merge SHA and target branch must still equal that SHA.
- [ ] Prove `PENDING` leaves fuse off and state pending.
- [ ] Prove `FAILED` records failure and engages existing `FuseStore` plus control audit in the same UoW; `DRIFTED` engages fuse and marks manual intervention.
- [ ] Implement using `RequiredCheckEvaluator`; do not add a second check owner.
- [ ] Run focused tests plus existing fuse/controller tests; require PASS.
- [ ] Commit `feat: add exact merge SHA canary gate`.

### Task 6: Deterministic revert candidate and exact-object PR reuse

**Files:**
- Modify: `src/meta_loop/infrastructure/github.py`
- Modify: `src/meta_loop/application/integration.py`
- Test: `tests/test_phase9_revert_publish.py`

**Interfaces:** recovery is allowed only for the exact `CANARY_FAILED` integration while fuse is engaged and governance authorizes `revert_publication`.

- [ ] Write RED tests for deterministic revert identity: parent = failed merge SHA; tree = original base tree SHA; ref = `refs/heads/meta-loop/revert/<integration_id>`; fixed metadata yields stable head SHA.
- [ ] Write RED tests requiring target still at failed merge SHA and rejecting drift, duplicate conflicting intents, fuse-off recovery, or unauthorized revert.
- [ ] Refactor the Phase 8 GitHub publisher only as needed to expose one exact-object/ref/PR owner reusable by publication and revert. Do not duplicate blob/tree/commit/ref creation logic.
- [ ] Persist `REVERT_REQUESTED` before remote I/O; reconcile existing exact ref/PR after timeout rather than create duplicates.
- [ ] Run Phase 8 publisher tests + new revert tests; require PASS.
- [ ] Commit `feat: add deterministic protected revert publication`.

### Task 7: Revert checks and protected rollback merge

**Files:**
- Modify: `src/meta_loop/application/integration.py`
- Modify: `src/meta_loop/infrastructure/github_integration.py`
- Test: `tests/test_phase9_revert_merge.py`

**Interfaces:** `revert_checks(integration_id)` uses the shared evaluator; `revert_merge(integration_id)` uses protected squash and exact expected revert head.

- [ ] Write RED tests requiring exact revert-head checks to pass before rollback merge.
- [ ] Write RED recovery tests for timeout after rollback merge and reject target drift.
- [ ] Implement rollback reconciliation proving pre-rollback target = failed merge SHA, rollback commit parent = failed merge SHA, rollback tree = original base tree, and target = rollback merge SHA.
- [ ] Verify state reaches `REVERT_MERGED` but fuse remains engaged; no recursive auto-revert or Task completion.
- [ ] Run focused tests; require PASS.
- [ ] Commit `feat: complete Phase 9 protected rollback`.

### Task 8: CLI, security review, completion gate, PR

**Files:**
- Modify: `src/meta_loop/cli/main.py`
- Modify: `.env.example`
- Create: `tests/test_phase9_cli.py`
- Create: `docs/reports/PHASE9_SECURITY_REVIEW.md`
- Create: `docs/reports/PHASE9_COMPLETION_REPORT.md`
- Modify: `README.md`

- [ ] Write RED parser/dispatch tests for `integration merge|canary|status|revert-publish|revert-checks|revert-merge`, schema-v1 envelopes, redaction, and absence of force/delete/complete/release options.
- [ ] Wire runtime composition fail-closed: GitHub writes require the existing explicit write-enable control plus governance; no new permissive defaults.
- [ ] Run `python -m pytest -q -rs` with disposable PostgreSQL 16 and require zero failures/skips for Phase 9 integration tests.
- [ ] Run `python -m compileall -q src tests` and repository diff/boundary checks.
- [ ] Audit for duplicate Integration/check/Git/fuse owners, GitHub bypass/force/delete/direct-ref behavior, secrets/path/DSN leakage, network I/O inside UoW, Phase 10 creep, and unresolved Critical/High findings.
- [ ] Write security/completion reports and update README only after evidence is green.
- [ ] Open a PR from `codex/phase9-implementation` to protected `main`; merge only after PR-triggered `test` passes and head SHA is unchanged.
