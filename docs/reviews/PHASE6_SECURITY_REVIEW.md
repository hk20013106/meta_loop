# Phase 6 security review

Critical: none. High: none.

Medium: local read-only enforcement uses portable owner-write permission bits.
Windows ACL isolation is not claimed. This environment did not permit creating
a directory symlink, so the test exercised the adapter's fail-closed symlink
branch; the adapter still rejects a real symlink whenever the OS exposes one.

Low: an interrupted process between local worktree creation and UoW commit can
leave an unledgered managed path. Allocation IDs are stable, retries validate
the fixed SHA, and release is idempotent; no automatic broad filesystem scan or
delete is performed.

Resolved controls: canonical request/idempotency conflict checks; explicit
repository allowlist; fixed SHA and existing-head drift rejection; managed-root
and symlink checks; R3 proposal-only/read-only restriction; fail-closed fuse
and implementation governance; UoW task-event-ledger atomicity; safe release;
application-layer isolation from subprocess, credentials and sibling imports.

Not implemented: GitHub read/write, remote branches, CI, merge, canary,
revert, real Hermes and research execution.
