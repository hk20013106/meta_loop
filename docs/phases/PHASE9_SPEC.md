# Phase 9 specification: protected merge, canary, and revert

Phase 9 is the post-publication integration boundary. It consumes one Phase 8 publication whose exact PR head is prepared, verified, approved, published, and currently green. It may merge that exact PR through the protected target branch, observe post-merge checks on the exact merge SHA, and on terminal failure restore the exact pre-merge tree through a protected revert PR.

It does not deploy traffic, complete the Task, close the source Issue, delete branches, author governance policy, or implement Phase 10 unattended E2E.

## Single owner

Phase 9 adds one durable `IntegrationLedger` as the single owner of post-publication lifecycle state. Phase 8 publication/approval/effect/check records remain immutable canonical inputs.

State flow:

```text
MERGE_REQUESTED -> MERGED -> CANARY_PENDING -> CANARY_PASSED
                                      |
                                      +-> CANARY_FAILED -> REVERT_REQUESTED
                                                           -> REVERT_PUBLISHED
                                                           -> REVERT_CHECKED
                                                           -> REVERT_MERGED
```

Ambiguous remote drift enters `MANUAL_INTERVENTION_REQUIRED`; it is never repaired by force, reset, delete, or semantic equivalence.

Only one unresolved integration may own a `(repository_name, target_branch)` at a time.

## Preconditions

Merge is eligible only when all are true:

- Task remains `READY_TO_PUBLISH`, risk is R0-R2, and Task/version/repository/base identity is unchanged;
- fuse is off;
- governance authorizes `merge_publication`;
- Phase 8 intent, prepared head, verifier result, approval, publication receipt, PR, patch/base/tree/head identities all agree;
- a fresh Phase 8 required-check evaluation for the exact PR head passes;
- target branch currently points to the Phase 8 base SHA;
- the target repository is not the Meta Loop control repository itself. Self-modification remains a separate meta-meta process.

Historical success or an equivalent tree is not sufficient.

## Integration identity

`IntegrationIntent` is immutable schema-v1 state containing at least:

- integration/publication/effect/Task IDs;
- repository and target branch;
- PR number;
- base SHA and base tree SHA;
- approved head SHA and prepared tree SHA;
- deterministic publication ref;
- governance revision;
- expected Task version/event sequence.

The base tree SHA is recorded before merge so rollback restores exact Git bytes rather than reconstructing meaning.

Migration `0010_phase9_integrations.sql` adds the IntegrationLedger and append-only normalized effect/observation facts. Earlier migrations are immutable. External I/O never occurs inside a database transaction.

## Protected merge

Phase 9 supports one merge method only: `squash`.

Immediately before merge the service must freshly validate fuse/governance, Phase 8 identity, PR open state, PR base SHA/branch, PR head SHA/ref, target branch at the immutable base SHA, and current exact-head checks.

`MERGE_REQUESTED` is committed before the GitHub write. The merge request must bind the expected PR head SHA and use normal branch protection; no administrative bypass or direct target-ref mutation is allowed.

After success or an uncertain response, reconciliation reads GitHub first. A valid `MergeReceipt` must prove:

- merged PR is the durable PR;
- merged PR head is the approved Phase 8 head;
- merge commit has exactly one parent equal to the original base SHA;
- merge commit tree equals the Phase 8 prepared tree SHA;
- target branch points to the merge SHA.

A timeout must not cause a second merge if remote state already proves the exact first merge.

Any parent/tree/base/head mismatch engages the fuse and enters `MANUAL_INTERVENTION_REQUIRED`.

## Post-merge canary

For Phase 9, canary means **post-merge GitHub CI on the exact merge SHA**. It is not production deployment or traffic shifting.

The existing Phase 8 check normalization/evaluation logic must be generalized and reused; Phase 9 must not create another check authority. Canary check names are explicit configuration and have no implicit fallback.

A canary evaluation first verifies target branch still equals the recorded merge SHA, then reads checks for that SHA only.

Results:

- `PENDING`: required checks are missing/queued/in-progress; remain pending and do not revert;
- `PASSED`: all required checks completed successfully;
- `FAILED`: required checks are terminal and at least one is non-success;
- `DRIFTED`: target branch moved before canary resolved.

`FAILED` atomically records failure and engages the existing fuse/control audit. `DRIFTED` also engages the fuse and enters manual intervention, with no automatic revert because branch context changed.

`PASSED` ends Phase 9 integration successfully, but Task stays `READY_TO_PUBLISH`; completion/Issue closure are deferred.

## Revert while fuse is engaged

Normal merge requires fuse off. After canary failure the fuse stays engaged. The only Phase 9 write allowed while engaged is recovery of that exact failed integration, and it requires separate governance capability `revert_publication`.

Automatic revert is allowed only when:

- state is terminal `CANARY_FAILED`;
- fuse is engaged;
- revert governance is authorized;
- target branch still points exactly to the failed merge SHA;
- original base SHA/tree are available;
- no conflicting revert intent exists.

The deterministic revert candidate is defined by exact Git identity:

- parent = failed merge SHA;
- tree = original base tree SHA;
- ref = `refs/heads/meta-loop/revert/<integration_id>`;
- fixed deterministic commit metadata.

No inverse patch is regenerated from working files and no semantic re-hashing is allowed.

The revert PR must reuse/refactor the Phase 8 exact-object PR publisher rather than introduce another Git-object owner. Its exact head must pass explicitly configured revert checks using the same check evaluator.

Rollback merge is also protected `squash`. A valid final rollback proves:

- pre-rollback target branch was the failed merge SHA;
- rollback commit parent is the failed merge SHA;
- rollback tree equals the recorded original base tree SHA;
- target branch points to the rollback merge SHA.

After `REVERT_MERGED`, the fuse remains engaged. Release is always the existing explicit governance action. There is no recursive auto-revert.

## Reuse constraints

- Phase 8 publication/approval/effect/check facts remain canonical inputs.
- One normalized check evaluator serves PR head, merge SHA, and revert head.
- One exact-object GitHub PR publisher owns Git object/ref/PR identity.
- One IntegrationLedger owns all Phase 9 state.
- Existing FuseStore/control audit remains the only emergency-stop owner.
- Hash bytes, not meaning; never create an equivalent downstream identity.

## CLI

Schema-v1 CLI adds explicit one-step operations:

```text
meta-loop integration merge --integration-id ID --publication-id ID --effect-id ID --json
meta-loop integration canary --integration-id ID --json
meta-loop integration status --integration-id ID --json
meta-loop integration revert-publish --integration-id ID --json
meta-loop integration revert-checks --integration-id ID --json
meta-loop integration revert-merge --integration-id ID --json
```

There are no force/bypass/reset/delete/complete-task/close-issue/release-fuse options. Repeated calls reconcile idempotently or fail closed on drift.

## Crash recovery and concurrency

Acceptance must cover:

- crash after durable merge intent but before remote merge;
- timeout after remote merge but before receipt persistence;
- concurrent merge attempts for one integration;
- two integrations targeting the same repository/branch;
- crash after canary failure/fuse engagement;
- crash after revert intent but before revert PR;
- timeout after revert PR creation;
- timeout after rollback merge.

Retries inspect exact remote state first. Duplicate merge, duplicate revert PR, second rollback merge, force mutation, or acceptance of changed identity are failures.

## Tests and acceptance

Phase 9 is fake-first for GitHub writes; acceptance must not merge or revert a real external target repository.

Required coverage includes DTO/canonicalization, memory/PostgreSQL IntegrationLedger contracts, one-active-integration-per-target locking, governance/fuse/R3/stale-state rejection, fresh PR checks, PR/base/head/tree drift, squash-only merge, exact merge reconciliation, canary pending/pass/fail/drift, atomic fuse engagement, recovery allowed under fuse only with revert authorization, deterministic revert identity, revert PR/check/merge idempotency, rollback exact tree restoration, CLI redaction, migration `0009 -> 0010`, disposable PostgreSQL 16, full test suite, compileall, diff check, and boundary scans for duplicate authority and Phase 10 creep.

Completion is blocked by unresolved Critical/High findings, especially: bypassing Phase 8 approval/checks; accepting Git identity drift; non-squash/direct/forced target mutation; canary on the wrong SHA; failure without fuse engagement; semantic rollback; reverting after target branch drift; Meta Loop self-modification; database transaction across network I/O; duplicate effect after timeout; duplicate check/Git/fuse/state owners; treating pending canary as failure; auto-releasing fuse; Task completion or Issue closure in Phase 9.

## Non-goals

Phase 9 does not implement production deployment/traffic canary, Task `COMPLETED`, Issue closure, branch deletion, governance authoring, webhook/background scheduler, real Hermes execution, recursive rollback, Meta Loop self-modification, or Phase 10 unattended E2E.

## Completion criteria

Phase 9 is complete only when:

1. this spec is approved;
2. migration `0010` plus memory/PostgreSQL adapters satisfy one IntegrationLedger contract;
3. protected squash merge, exact reconciliation, merge-SHA canary, fuse-on-failure, deterministic revert PR/checks, and protected rollback merge are implemented fake-first;
4. no duplicate identity/effect/check/fuse authority exists;
5. disposable PostgreSQL 16 Phase 9 tests have zero failures/skips;
6. full suite, compile gate, diff check, and security review pass with no unresolved Critical/High finding;
7. the Phase 9 feature branch reaches protected `main` only through a green PR.

Phase 10 begins only after Phase 9 is accepted. Terminal Task/source lifecycle decisions belong to Phase 10.