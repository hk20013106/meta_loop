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
