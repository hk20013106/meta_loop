# Phase 4–5 security review

Critical: none. High: none. Medium: real Hermes adapter validation is deferred;
the shipped runner is a deterministic fake and cannot execute a process.
Low: artifact logical names remain metadata and are not used as filesystem paths.

Resolved controls: canonical session/result idempotency, stable worker roles,
R3 implementer rejection, fuse enforcement, lease ownership at completion,
append-only task evidence, CAS-reference-only events, UoW rollback, and no
credential, subprocess, network, sibling-repository or worktree dependency.
