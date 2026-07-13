- Deployment: Windows + WSL2 + Docker Desktop
- Controller: Python
- Queue/state: PostgreSQL with SKIP LOCKED
- Agent runtime: Hermes CLI via WSL2 host runner
- Roles: planner, design reviewer, implementer, patch reviewer
- Models: GLM-5.2 / DeepSeek V4 Flash / Hy3
- Task source: GitHub Issue template
- Trigger authority: Kai only
- GitHub identity: personal fine-grained PAT for MVP
- Artifact store: local WSL2 SHA-256 CAS
- CI: ephemeral Docker verifier for R0–R2
- R3: no automatic implementation; read-only patch proposal only
- Review retries: maximum 3
- New initiatives: 72-hour experimental branch
- Meta-loop self-modification: separate meta-meta process

## ADR-001 — Phase 1 split

- **Decision:** Phase 1 is split into 1A (pure domain and ports) and 1B
  (PostgreSQL persistence and queue adapter).
- **Context:** The original one-line phase mixed infrastructure-independent
  rules with database concurrency behavior.
- **Alternatives:** One combined milestone; defer queue semantics.
- **Reason:** 1A can be exhaustively tested without a database while 1B keeps
  its transaction boundary explicit.
- **Consequences:** Phase 2 remains Phase 2; no later phases are renumbered.
- **Status:** accepted.

## ADR-002 — Governance absence fails closed

- **Decision:** Missing governance revision blocks execution- and
  publication-authorizing transitions. Receiving, validating, planning, and
  R3 proposal preparation remain representable.
- **Context:** `governance_root` contains no policy or schema.
- **Alternatives:** allow by default; block every task.
- **Reason:** No authority can be inferred, while offline domain work remains
  testable.
- **Consequences:** Phase 1 does not create governance policy.
- **Status:** accepted.

## ADR-003 — PostgreSQL migrations and queue defaults

- **Decision:** Use ordered SQL migrations; default lease is 300 seconds,
  default maximum attempts is 3, and retry backoff is 30 seconds exponential
  capped at 15 minutes.
- **Context:** PostgreSQL and `SKIP LOCKED` are already decided; no ORM is.
- **Alternatives:** Alembic/ORM; unversioned schema setup.
- **Reason:** SQL is auditable and preserves the database contract directly.
- **Consequences:** Phase 1B adapters use DB-API boundaries without importing
  a PostgreSQL driver in the domain.
- **Status:** accepted.

## ADR-004 — Local controller and fuse

- **Decision:** Phase 2 uses a Python application controller and argparse CLI;
  the local fuse is persisted in PostgreSQL with append-only control audit.
- **Consequences:** no REST server, subprocess execution, or provider authority
  is introduced; unauthorised release fails closed.

## ADR-005 — Local CAS and catalog split

- **Decision:** blobs use a local SHA-256 filesystem CAS while PostgreSQL stores
  only artifact metadata and references.
- **Consequences:** database rollback can leave report-only orphan blobs; blobs
  never enter events or logs, and secret-marked input is rejected before write.

## ADR-006 — Fake-first Hermes runner protocol

- **Decision:** Phase 4 ships a provider-neutral, persistent session protocol
  with a deterministic fake runner; it does not invoke Hermes or WSL.
- **Reason:** no approved executable runtime or credentials are available for
  a safe real integration gate.
- **Consequences:** runner requests exclude secrets, commands, paths and
  environments. Real adapter validation is explicitly deferred.
