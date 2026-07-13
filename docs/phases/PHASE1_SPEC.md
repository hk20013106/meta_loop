# Phase 1 specification

## 1A domain contract

`Task` is immutable and versioned: task ID, `TaskSource(kind, reference)`,
`RepositoryTarget(name, revision)`, risk assessment, status, created time,
rework round, and immutable artifact references. IDs are non-empty strings in
the first schema; `IdGenerator` is the deterministic/prod creation boundary.
`Clock` supplies time. All serialization is JSON-shaped with
`schema_version=1`; unknown versions fail closed.

`RiskAssessment` holds proposed, independently validated, and effective
`RiskClass` values, escalation reason, and optional governance revision.
Effective risk is never lower. R3 always routes from design review to
`PROPOSAL_READY`; an attempted implementation blocks the task.

`ArtifactRef` contains lowercase SHA-256 digest, logical name, media type, and
schema version. It is a reference only; no file is written in Phase 1.

Task lifecycle is `RECEIVED → VALIDATED → PLANNED → DESIGN_REVIEW →
IMPLEMENTING → PREFLIGHT → PATCH_REVIEW → READY_TO_PUBLISH`. R3 instead uses
`DESIGN_REVIEW → PROPOSAL_READY`. Review may create `REWORK_REQUIRED`; after
three rounds the task is `BLOCKED`. `FAILED` and `COMPLETED` are terminal;
unblocking requires an explicit event and fresh validator check. Each ordinary
transition requires its named role. Execution/publish transitions require an
authorizing governance revision; otherwise the task becomes `BLOCKED`.

An `Event` contains event/task IDs, controlled type, time, actor, immutable
payload, schema version, causation/correlation IDs, and per-task sequence.
`(task_id, sequence)` is unique. A duplicate exact event ID is idempotent;
reusing an event ID with different contents is rejected. Historical events are
never updated. Snapshots cannot be treated as factual history.

## ports and ownership

`TaskRepository.get/save(expected_version)`, `EventStore.append(expected_sequence)/read`,
and `TaskQueue.enqueue/claim/heartbeat/complete/fail/recover_expired` have
optimistic-conflict, sequence-conflict, lease-lost, and not-found errors.
`ArtifactStore.register/get`, `Clock.now`, `IdGenerator.new`, and
`GovernanceReader.read_revision/authorize` are explicit boundaries. Phase 1A
provides in-memory test doubles; Phase 1B supplies PostgreSQL task/event/queue
adapters. Domain code imports none of them.

`UnitOfWork` owns `tasks`, `events`, and `queue`. A PostgreSQL UoW exposes all
three over one connection and commits only when explicitly requested; exception
exit or an uncommitted exit rolls back. The in-memory UoW has identical
copy-on-write commit semantics. Application services, rather than domain
objects or individual adapters, coordinate task/event/queue changes.

Application transition services require evidence. Execution and publication
transitions call `GovernanceReader` for the stored revision and the respective
`implement` or `publish` capability; absent or unverifiable authorization
writes an explicit `BLOCKED` transition. Terminal tasks cannot transition
again.

## 1B queue and transaction contract

Queue records include task ID, state, priority, availability, claim fields,
attempt/max attempts, error, idempotency key, and timestamps. Claim eligibility
requires availability, no live lease, and attempts below maximum. A single
transaction locks one candidate with `FOR UPDATE SKIP LOCKED`, increments the
attempt, and records the lease. Exactly one worker can claim a task. Expired
leases become claimable. Retry schedules 30s exponential backoff capped at 15m;
the third exhausted attempt terminalizes failure. Queue claim is task-level,
not a semantic resource lock.

State + event, claim + attempt, completion + terminal queue action, retry +
failure event, and artifact registration + reference event must be atomic. The
event table database trigger rejects updates/deletes. Integration tests require
an isolated disposable PostgreSQL database; no test may use research data.

The event store first locks the parent task row, then reads and validates the
next sequence. An exact canonical duplicate event ID is idempotent; changed
content with that ID is rejected. Queue methods return provider-neutral result
DTOs; ownership or expiry violations raise `LeaseLostError`. One active queue
row exists per task, and conflicting enqueue intent is rejected.

Completion validates both worker identity and an unexpired lease. Migration
ledger entries must be a contiguous prefix of the local ordered plan; unknown,
out-of-order, and checksum-mismatched entries are rejected.

## proposed implementation map

- `domain/{enums,errors,artifacts,events,task}.py`: pure schema, validation,
  and transitions; unit tests only.
- `application/ports.py`: dependency-free protocol signatures.
- `infrastructure/memory.py`: deterministic 1A test doubles.
- `infrastructure/postgres.py` and `migrations/0001_phase1b_core.sql`:
  DB-API adapter and database schema; integration tests only.
- `infrastructure/migrations.py`: ordered SQL migration ledger and checksum
  validation.
- `serialization/codecs.py`: schema-versioned event JSON codec.
