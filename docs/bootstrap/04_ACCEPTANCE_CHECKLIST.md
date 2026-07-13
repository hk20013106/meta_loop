[x] Phase 1A domain validates all required fields and controlled values
[x] Phase 1A legal and illegal state transitions are covered
[x] Phase 1A R3 cannot enter implementation
[x] Phase 1A events are append-only and sequence-conflict checked
[x] Phase 1A serialization rejects unknown schema versions
[x] Phase 1A ports and in-memory adapters are deterministic
[x] Phase 1B migrations create tasks, events, and queue tables
[x] Phase 1B PostgreSQL queue supports lease, retry and SKIP LOCKED
[x] Phase 1B event store prevents update/delete
[x] Phase 1B integration tests prove exclusive claims and lease recovery
[x] Phase 2 synthetic intake is atomic and idempotent
[x] Phase 2 local CLI entry point and schema-versioned output exist
[x] Phase 2 fuse schema uses persistent state and append-only audit events
[ ] Workers cannot directly mutate task state
[ ] Hermes sessions have isolated HOME and no shared memory
[ ] Implementer cannot access GitHub PAT
[ ] Planner and reviewers mount repositories read-only
[ ] Review approval binds to exact head commit
[ ] Maximum review retries = 3
[ ] R3 tasks cannot enter automatic implementation
[ ] R3 patch proposal is generated without applying it
[ ] R0–R2 CI uses disposable containers
[ ] GitHub Issue trigger validates Kai’s label event
[ ] All artifacts are content-addressed with SHA-256
[ ] Controller can reconcile GitHub state
[ ] Failed canary produces a controlled revert path
