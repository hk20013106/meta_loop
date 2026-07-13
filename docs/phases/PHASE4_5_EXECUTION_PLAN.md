# Phase 4–5 execution plan

Phase 4 is the provider-neutral Hermes runner protocol. Phase 5 builds
planner, reviewer, implementer and patch-reviewer orchestration on that
protocol. Phase 5 starts only from the clean Phase 4 commit and successful
disposable PostgreSQL gate.

Both phases use schema-v1 canonical records, the existing UoW, task event
stream, queue lease and CAS catalog. They do not create worktrees, call shell
or WSL processes, access networks, read credentials, ingest GitHub events,
publish branches or create pull requests. Git worktree management and GitHub
ingestion remain Phase 6 and Phase 7 respectively.
