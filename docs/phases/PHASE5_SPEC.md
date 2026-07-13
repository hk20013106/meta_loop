# Phase 5 specification: role-isolated task workers

Workers have stable local IDs and one controlled planner, design-reviewer,
implementer or patch-reviewer role. They only run a persisted Phase 4 session
and return a structured outcome; they never mutate Task, EventStore or Queue.
`ResultPublicationService` validates the successful session, role, lease and
expected versions, writes output through the Phase 3 CAS, then atomically
registers its reference, records evidence, applies the domain transition,
completes the queue lease and stores the idempotent result ledger entry.

Results contain only a bounded summary and references; raw output, prompts,
credentials, environments and paths are forbidden. R3 cannot request an
implementer session. Fuse blocks new sessions/runs but does not revoke leases.
Cancellation requires `cancel_execution` governance authorization and fails
closed when governance is absent. Worktrees, processes, networks, GitHub and
real Hermes invocation remain out of scope.
