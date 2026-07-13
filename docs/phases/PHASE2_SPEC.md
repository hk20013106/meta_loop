# Phase 2 specification: Controller and local CLI

Phase 2 adds a local application controller above Phase 1 ports. It creates
synthetic tasks atomically with their received event, canonical intake-ledger
record, and optional queue row. `request_id` is the intake idempotency key;
the ledger stores canonical schema-v1 request JSON and rejects changed content.
It does not duplicate domain transitions, execute workers, invoke a shell,
call GitHub, or access a sibling repository.

`meta-loop` and `python -m meta_loop` expose `doctor`, `start`, `status`,
`tasks`, and `fuse`. Human output is deterministic; JSON output has
`schema_version: 1`; controlled failures use stderr and a nonzero exit code.
`start --request` accepts only schema-v1 synthetic JSON. Secrets and DSNs are
never rendered.

The persistent fuse is represented by a singleton database row and an
append-only control-event table. Engage blocks new controller starts and
claims; existing leases are untouched. Release requires `fuse_release`
governance authorization and otherwise fails closed. The UoW owns one commit
boundary; uncommitted intake, fuse and audit mutations roll back together.

`doctor [--json]` is read-only and reports database/migration, governance and
CAS-root health without rendering configuration values. `start`, `status`,
`tasks`, and `fuse` use PostgreSQL composition from `META_LOOP_POSTGRES_DSN`.
All JSON responses have `schema_version: 1`; time values are timezone-aware
ISO-8601; controlled errors go to stderr with a stable nonzero result.

Non-goals: REST, GitHub, Hermes, workers, CAS blobs, merge/revert, real data,
and Phase 3+ behavior.
