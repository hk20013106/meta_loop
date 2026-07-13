Phase 0  Repository/bootstrap
Phase 1A Core domain model and ports
Phase 1B PostgreSQL persistence and queue adapter
Phase 2  Controller and CLI
Phase 3  CAS artifact store
Phase 4  Hermes runner protocol
Phase 5  Planner/reviewer/implementer workers
Phase 6  Git worktree manager
Phase 7  GitHub Issue ingestion
Phase 8  PR publisher and CI integration
Phase 9  auto-merge, canary and revert
Phase 10 end-to-end hardening



每个 phase 都包含：

Goal
Inputs
Files/modules
Required behavior
Explicit non-goals
Tests
Completion criteria
Commands to verify
Stop conditions

## Phase 2

- **Goal:** Provide a single application controller and local argparse CLI.
- **Inputs:** Phase 1 ports/UoW, PostgreSQL adapters, and fail-closed governance.
- **Required behavior:** atomic synthetic intake/event/enqueue, query surface,
  persistent fuse audit, schema-v1 JSON and no credential disclosure.
- **Tests:** controller/CLI unit tests plus disposable PostgreSQL migration gate.
- **Non-goals:** GitHub, Hermes, execution, REST, CAS blobs, and Phase 3+.

## Phase 3

- **Goal:** Provide a local SHA-256 content-addressed artifact store.
- **Inputs:** Phase 2 UoW, Task/event ports, and a configured CAS root.
- **Required behavior:** verified atomic blob writes, metadata catalog, atomic
  task/reference event registration, secret rejection, and report-only orphan
  detection.
- **Tests:** CAS unit/contract tests plus disposable PostgreSQL catalog gate.
- **Non-goals:** remote storage, deletion, encryption, workers, and Phase 4+.

## Phase 4

- **Goal:** Provide a provider-neutral Hermes runner session protocol.
- **Inputs:** Phase 1 UoW/event stream, Phase 2 fuse and Phase 3 references.
- **Required behavior:** canonical session idempotency, task/event atomicity,
  R3 implementer rejection and credential-free fake-runner contracts.
- **Tests:** memory and disposable PostgreSQL session-ledger tests.
- **Non-goals:** real Hermes execution, workers, shell/WSL, worktrees, GitHub,
  CI and publishing.

## Phase 5

- **Goal:** Provide role-isolated fake-runner worker orchestration.
- **Inputs:** Phase 4 sessions, queue leases and Phase 3 CAS/catalog.
- **Required behavior:** worker/result idempotency, role and R3 enforcement,
  CAS-reference-only publication, lease-bound completion and fail-closed
  governance cancellation.
- **Tests:** memory contracts and disposable PostgreSQL migration/full-suite
  gate.
- **Non-goals:** worktrees, shell/WSL, networks, GitHub, real Hermes and CI.

## Phase 6

- **Goal:** Provide a provider-neutral managed Git worktree boundary.
- **Inputs:** Phase 5 task/session/queue contracts, a verified source SHA and
  explicit local repository configuration.
- **Required behavior:** canonical allocation idempotency, UoW ledger/event
  atomicity, role/R3/fuse/governance checks, path-safe detached worktrees and
  idempotent release.
- **Tests:** fake/memory contract plus temporary local Git and disposable
  PostgreSQL integration tests.
- **Non-goals:** GitHub, CI, remote branches, publication, merge, revert,
  real Hermes and real research execution.

## Phase 1A

- **Goal:** Define and test the domain, state transitions, risk, events,
  serialization, ports, and in-memory adapters.
- **Inputs:** Phase 0 boundary and ADR-001/002.
- **Files/modules:** `domain/`, `application/ports.py`, memory adapter,
  serialization codecs, and unit tests.
- **Required behavior:** controlled values, fail-closed validation, append-only
  event contract, deterministic time/IDs, and no sibling-runtime dependency.
- **Explicit non-goals:** PostgreSQL driver/migration execution, GitHub,
  Hermes, Docker, CAS writes, and governance authoring.
- **Tests:** domain, event, serialization, memory adapter, and boundary tests.
- **Completion criteria:** all 1A acceptance tests and full suite pass; no
  PostgreSQL dependency; no credentials or sibling changes.
- **Commands:** `python -m pytest -q`; `git diff --check`.
- **Stop conditions:** missing domain rule or fail-closed validation defect.

## Phase 1B

- **Goal:** Persist the 1A ports in PostgreSQL and prove queue concurrency.
- **Inputs:** Phase 1A contracts and a disposable PostgreSQL database.
- **Files/modules:** ordered SQL migrations and DB-API PostgreSQL adapters.
- **Required behavior:** transactional task/event/queue boundaries,
  append-only events, idempotent enqueue, lease recovery, retries, and
  `FOR UPDATE SKIP LOCKED` claims.
- **Explicit non-goals:** all Phase 2+ runtime capabilities and semantic
  resource locking.
- **Tests:** disposable-database integration tests for concurrent claims,
  recovery, transaction rollback, and event/task consistency.
- **Completion criteria:** adapters satisfy ports and all integration tests pass
  against an isolated database.
- **Commands:** Phase 1A commands plus the integration test selection.
- **Stop conditions:** no disposable database, non-atomic transaction, or
  failure to prove lease/concurrency semantics.
