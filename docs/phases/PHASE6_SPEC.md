# Phase 6 specification: Git worktree manager

`WorkspaceRequest` is schema version 1 and contains an allocation ID, task ID,
role, controlled purpose, full source revision, expected task/event versions,
correlation ID and optional governance revision. Its canonical form has no
filesystem path, credential, command, environment or arbitrary Git option.
Unknown schema versions and malformed revisions are rejected.

`WorkspaceRecord` contains only provider-neutral repository name, the request,
read-only policy and `active` or `released` state. `WorkspaceReceipt` reports
allocation ID, state and idempotent creation. `WorkspaceLedger` persists these
records in the UoW, where one active allocation per `(task, purpose)` is
allowed. Allocation and `WORKSPACE_ALLOCATED` event append are atomic;
release and `WORKSPACE_RELEASED` event append are atomic.

`WorkspaceAllocationService` checks idempotency before fuse, validates task and
event versions, then applies role/risk policy. Planner may request planning;
design and patch reviewers may request review; implementer may request
implementation only in `IMPLEMENTING` with authorized governance. R3 accepts
only `PROPOSAL`, has read-only policy, and rejects implementer requests.

`FakeWorkspaceManager` has no filesystem capability. `LocalGitWorkspaceManager`
is opt-in infrastructure: it maps only configured repository names to local
Git roots, resolves an exact commit, creates a detached worktree under the
configured managed root, applies read-only permission bits when required, and
removes only a ledger-owned managed workspace. It does not read credentials or
perform network operations. Windows ACL semantics remain an OS limitation; the
adapter applies the portable owner-write bit and does not claim ACL isolation.
