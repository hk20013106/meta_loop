# Phase 4 report

Status: `COMPLETE`

Phase 4 establishes the schema-v1, provider-neutral Hermes runner session
protocol. Requests are canonical and idempotent; the ledger, task request event
and task validation share one UoW. `FakeRunner` is deterministic and has no
process, filesystem, environment, credential or task-mutation capability.

Verification on disposable PostgreSQL 16: `70 passed, 0 skipped`. Migration
`0004_phase4_runner_sessions.sql` applied with the ordered ledger; memory and
PostgreSQL contracts cover duplicate requests, changed-payload rejection and
transaction rollback. The task container used for verification was removed.

Real Hermes/WSL execution, workers, worktrees, GitHub, CI and publishing remain
out of scope. No sibling repository or governance directory was modified.
