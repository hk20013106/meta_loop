# Phase 6 report

Status: `COMPLETE`

Authoritative title: Git worktree manager.

Phase 6 adds schema-v1 canonical workspace requests, receipts and records; a
UoW workspace ledger with migration `0006_phase6_workspaces.sql`; atomic safe
workspace task events; deterministic fake management; and an opt-in local Git
adapter. The local adapter accepts only configured local repositories and a
managed root, checks exact commits and drift, rejects escape attempts, and
creates detached worktrees. Planner/reviewer and R3 proposal workspaces apply
a read-only policy. Implementer allocation requires `workspace_implementation`
governance and rejects absent or invalid policy.

Enabled capabilities: fake workspace contracts and local temporary-repository
Git worktree create/release. Disabled capabilities: GitHub read/write, remote
branches, push, PRs, CI, automatic merge/revert, real Hermes and real research
execution.

Verification on disposable PostgreSQL 16: `python -m pytest -q -rs` produced
`88 passed, 0 failed, 0 skipped`. `python -m compileall -q src tests` and
`git diff --check` passed. The local Git adapter was exercised only with
temporary repositories; no external repository integration was performed.

Security review: no unresolved Critical or High findings. The Windows
read-only/ACL and unavailable real-symlink-test limitations are recorded as
Medium in `docs/reviews/PHASE6_SECURITY_REVIEW.md`.
