# Phase 8 specification: PR publisher and CI integration

Phase 8 is an opt-in, fail-closed publication boundary.  It turns exactly one
previously registered, Task-attached `text/x-diff` ArtifactRef into a
deterministic candidate, verifies that candidate locally, records an approval
bound to its exact Git head, and records GitHub publication/check observations.
It does not contact real GitHub during acceptance and it does not implement any
Phase 9 behavior.

## Scope and ordering

Phase 8 starts only after the Phase 7 integration baseline has passed its
PostgreSQL, compile, diff and security gates.  Phase 8 itself is serialized:

1. validate and prepare a candidate from a catalogued CAS patch;
2. verify that exact candidate in a disposable Docker container;
3. accept a successful `PATCH_REVIEWER` result approving that exact head;
4. durably request publication, then reconcile an optional GitHub REST effect;
5. read and record check runs for that same head.

The Task stays `READY_TO_PUBLISH` throughout.  Missing authorization, an
engaged fuse, stale task/event versions, an invalid risk state, source/CAS
drift, an unsafe patch, verifier failure, approval drift, remote drift, or a
non-success check state rejects the operation without widening capability.

## Public domain and application contracts

All Phase 8 value objects are schema-v1, canonically serializable, immutable at
the application boundary, and contain no local path, command, environment,
credential, DSN, raw blob, raw Issue body, or subprocess output.

- `PublicationIntent` records publication ID, Task ID, worker-result ID,
  unified-diff ArtifactRef digest, repository name, fixed base SHA, expected
  versions, correlation ID and governance revision.
- `PreparedHead` records publication ID, patch digest, fixed base SHA, computed
  tree SHA, computed head SHA and deterministic ref name.
- `VerificationResult` records the exact head SHA, pinned verifier image
  digest, verifier profile, controlled status and safe result code.
- `PublicationReviewDecision` records approval, Task, session and result IDs,
  approval ArtifactRef, patch/base/tree/head SHAs, `PATCH_REVIEWER` identity,
  decision and time.
- `PullRequestReceipt`, `PublicationReceipt`, `CheckRunObservation` and
  `CheckGateResult` are normalized receipts or observations, never provider
  response payloads.

`UnitOfWork` gains `PublicationLedger`, `PublicationApprovalStore`,
`PublicationEffectStore` and `CheckRunObservationStore`.  Their public
operations reserve a publication, append immutable approval/check facts, record
an external-effect intent, and reconcile only an identical receipt.  Memory and
PostgreSQL adapters implement the same conflict and idempotency semantics;
adapters do not expose database or HTTP exceptions through these contracts.

`PublicationPreparationService` requires all of R0--R2, Task state
`READY_TO_PUBLISH`, fuse off, a governance authorization/revision for
`prepare_publication`, a successful implementer result, and a Task-attached,
catalogued `text/x-diff` ArtifactRef.  It re-verifies the CAS digest and
canonical Task/result references before writing intent or candidate state.  R3
is excluded.  The patch is the sole publication source: a CLI filename,
workspace file, arbitrary diff or provider response can never substitute for
that ArtifactRef.

`PublicationApprovalService` has no arbitrary approval CLI.  It accepts only a
successful, stored `PATCH_REVIEWER` session/result and creates one append-only
approval whose Task, patch, base, tree and head exactly match `PreparedHead`.
Changing any of those values invalidates prior approval.

## Migration and durable recovery

`0007_phase7_issue_ingestion.sql` remains exclusively the Phase 7 append-only
Issue source-ingestion schema.  `0008_phase8_publications.sql` adds only
Phase 8 tables and constraints:

- immutable publication intent/record rows containing registered patch digest,
  base/tree/head SHAs, deterministic ref, optimistic state and canonical input;
- append-only exact-head approval rows;
- durable publish-effect intent and reconciliation rows; and
- append-only exact-head normalized check-run snapshots.

No Phase 8 database row stores a PAT, DSN, blob/diff content, raw GitHub
payload, filesystem path, command, environment or verifier log.  Every
in-database transition and its safe Task/event/ledger facts share one UoW.  A
network operation never occurs while a database transaction is held.

Before any write adapter call, the application commits a durable
`publish_requested` effect intent.  After an uncertain timeout or crash, retry
first inspects the deterministic ref and PR, then records a receipt only when
the observed remote tree/head/identity is identical.  Missing, mismatched or
ambiguous state fails closed; no retry force-updates, deletes or creates a
second publication.  A prior CAS write that is not committed with its intended
ledger facts is a report-only orphan and is never publication-visible.

## Candidate and patch safety boundary

The local candidate preparer is infrastructure-only.  It accepts a configured,
allowlisted repository and fixed base SHA, creates an owned disposable managed
worktree, and uses fixed Git metadata to derive deterministic tree and head
SHAs.  It rejects malformed or corrupt CAS content, absolute paths, parent
traversal, `.git` paths, binary patches, symlinks, submodules, rename/copy
operations and mode changes.  It receives neither PAT nor DSN and never turns
user-controlled text into shell syntax.  It removes only worktrees it owns;
repository, workspace and subprocess path details do not cross into domain,
application DTO, event or CLI output.

## Docker verifier boundary

The verifier operates only on the exact prepared head.  It is a disposable
Docker adapter using a configured pinned image digest and fixed allowlisted
argv.  Each run uses `--rm`, `--network none`, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, a non-root user, explicit resource/time
limits and bounded captured output.  It has no PAT, DSN, host credential,
Docker socket, writable host mount, or inherited arbitrary environment.

Only the controlled `VerificationResult` may leave the adapter.  Image tag
only references, network access, privilege escalation, mutable command
construction, timeout/output-limit bypass, non-zero verifier status or a head
mismatch block approval and publication.

## GitHub REST boundary

GitHub REST write support is disabled by default and fake-first.  Acceptance
uses fakes only and must make no real GitHub request.  When explicitly enabled
outside acceptance, the infrastructure adapter alone reads the PAT.  Git Data
APIs may create the fixed tree, commit and deterministic ref; the Pull Requests
REST API may create the PR.  It must compare every returned tree/head SHA with
`PreparedHead` before reconciling the durable effect.  It may not force-update,
delete, merge or close anything.

The read-only check adapter fetches check runs for the exact prepared/published
head SHA only.  Required check names are explicit configuration; every required
check must be `completed` with conclusion `success`.  Missing, pending,
neutral, skipped, cancelled, timed-out, action-required, failed, malformed or
stale observations block and are recorded only as normalized exact-head facts.

## CLI contract

The following commands use the existing stable schema-v1 JSON envelope,
`{command, ok, result|error}`, controlled error codes/messages and fixed exit
codes:

```text
meta-loop publication prepare --publication-id ID --task-id ID --worker-result-id ID --patch-digest HEX --json
meta-loop publication status --publication-id ID --json
meta-loop github publish --publication-id ID --json
meta-loop github checks --publication-id ID --json
```

There is deliberately no `approve` command.  JSON and text errors exclude PATs,
DSNs, blob/diff bytes, raw Issue text, paths, commands, argv, environment,
provider payloads, database exceptions and subprocess output.

## Acceptance, security and non-goals

Acceptance covers memory and PostgreSQL UoW behavior, fake GitHub transports,
CAS corruption, malformed/unsafe patches, deterministic SHA computation,
Docker verifier failures, approval/head drift, concurrent or crash-window
publish recovery, and every non-success check state.  Required gates are a
fresh disposable PostgreSQL 16 run with zero failures/skips, an actual task
created disposable Docker verifier run, `python -m pytest -q -rs`,
`python -m compileall -q src tests`, `git diff --check`, and boundary scans for
imports, secrets, network, subprocess use and Phase 9 capabilities.

The security review blocks completion for any unresolved Critical or High
finding.  Critical blockers include a non-CAS diff source; accepted base/head
drift; bypassed fuse, governance, verifier or approval; secret/path/command
leakage; privileged/networked/unpinned Docker; workflow modification; real
GitHub acceptance traffic; or any Phase 9 behavior.  High blockers include
missing durable effect/reconciliation, duplicate ref/PR after retry or drift,
a database transaction across network I/O, acceptance of stale checks, or a
mutable canonical publication ledger.

Non-goals: webhook hosting, target-repository workflow edits, real GitHub
writes during this phase's acceptance, merge, canary, revert, Issue closure,
Task completion, production deployment and every Phase 9-or-later capability.
