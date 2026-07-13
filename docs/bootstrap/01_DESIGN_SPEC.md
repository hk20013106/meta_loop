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
