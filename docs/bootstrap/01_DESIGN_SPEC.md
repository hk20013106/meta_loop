# Meta Loop design specification

## Phase 1 architecture

The domain is pure Python and depends on no PostgreSQL driver, GitHub,
Hermes, Docker, subprocess, filesystem layout, or sibling repository.
Application protocols provide `TaskRepository`, `EventStore`, `TaskQueue`,
`ArtifactStore`, `Clock`, `IdGenerator`, and `GovernanceReader`. Infrastructure
adapters implement protocols; no worker mutates task state directly.

Tasks carry provider-neutral source and repository references, controlled risk
assessment, status, version, rework round, timestamps, and artifact references.
Events are append-only facts with per-task sequence, actor, schema version,
causation/correlation identifiers, and validated payload. Derived state can be
rebuilt from events; snapshots are caches only.

`R0` through `R3` are controlled values. Each task records proposed, validated
and effective risk; effective risk cannot be lower than either input. R3 is
proposal-only and cannot transition to implementation. Missing governance
revision fails closed for execution and publication authorization.

## PostgreSQL queue contract

Phase 1B uses ordered SQL migrations for `tasks`, `task_events`, and
`task_queue`. Event rows cannot be updated or deleted. Claim is one atomic
transaction using `FOR UPDATE SKIP LOCKED`, priority descending and availability
ascending; it assigns a worker, sets a 300-second lease, and increments attempt.
Expired leases are recoverable. Enqueue is idempotent per task/idempotency key;
default maximum attempts is three with exponential backoff from 30 seconds to
15 minutes. Semantic resource locks are out of scope.

## Excluded Phase 1 capabilities

No GitHub, Hermes, Docker worker, PAT broker, true CAS writes, research-data
processing, automatic merge/revert, or governance-policy authoring is present.

## Phase 4 runner boundary

Phase 4 adds a schema-v1 provider-neutral runner session protocol. Requests
contain only controlled role/task metadata and Task-owned artifact references;
they cannot carry credentials, commands, environment variables, paths or raw
output. The session ledger and request event share a UoW. The included fake
runner is deterministic and isolated; real Hermes execution remains opt-in and
unimplemented.

## Phase 5 worker boundary

Phase 5 workers are role-constrained producers of structured results. The
application publication service alone validates a successful session and queue
lease, writes the output through CAS, then atomically records only its
ArtifactRef, task evidence, domain transition and result idempotency record.
Cancellation requires governance capability `cancel_execution`; missing policy
fails closed. Workers have no process, network, workspace or credential access.

## Phase 6 workspace boundary

Phase 6 separates application workspace allocation from local Git execution.
Canonical workspace requests have a stable allocation ID and exact source SHA,
but no path or command. Ledger state and safe task events are transactional;
the opt-in adapter receives only an allowlisted repository and creates a
detached worktree below one managed root. It rejects path/symlink escape and
head drift. Planner/reviewer policies are read-only, R3 is proposal-only, and
implementer access is governance-authorized or rejected.
