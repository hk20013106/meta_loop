# Phase 4 specification: Hermes runner protocol

`RunnerSessionRequest` is a schema-v1, provider-neutral contract containing a
stable session ID, task ID, controlled role, expected task/event versions,
Task-owned `ArtifactRef` inputs, a bounded timeout and a correlation ID. It
does not contain a token, DSN, environment, filesystem path, command, prompt
or raw output. Session IDs are idempotency keys: identical canonical requests
reuse the persisted receipt and changed content is rejected.

`RunnerSessionStore` persists canonical requests and safe outcomes. Its
PostgreSQL table has a task foreign key, schema-v1 checks, a session primary
key and row locking. `RunnerSessionService` checks the fuse, task/version,
event sequence, input ownership and the R3 implementer boundary; it appends a
`RUNNER_SESSION_REQUESTED` task event in the same UoW. A runner never mutates
Task, EventStore or Queue directly.

The shipped `FakeRunner` is deterministic and has no process, filesystem,
network or environment access. A real Hermes/WSL adapter is deliberately not
configured or invoked in Phase 4. Non-goals include workers, worktrees,
GitHub, CI, publishing and credentials.
