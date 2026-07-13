# Phase 6 execution plan: managed Git worktrees

Phase 6 delivers a provider-neutral, locally managed Git worktree boundary.
It begins only after the Phase 2–5 baseline is integrated on `master`.

Inputs are a Task, a full verified Git commit SHA, a controlled worker role,
task/event versions and a correlation ID. Deliverables are canonical allocation
records, an atomic PostgreSQL ledger/event boundary, a deterministic fake
adapter, and an opt-in local Git adapter tested only against temporary repos.

The manager accepts only an explicitly configured repository name and a fixed
managed root. It never accepts a path, Git argument, token, command or
environment from a request. It rejects sibling and governance roots, path or
symlink escapes, source/head drift and concurrent active task/purpose
allocations. Planner and reviewer workspaces are read-only; implementer
workspaces require governance `workspace_implementation`; R3 permits only a
read-only proposal workspace and never an implementer allocation.

Non-goals: GitHub, remote branches, push, PRs, CI, merge, canary, revert,
real Hermes execution and research execution. Database state and task events
share one UoW. Filesystem worktree creation/removal is idempotent and
compensated or retried through its stable allocation ID; it is not a database
transaction.
