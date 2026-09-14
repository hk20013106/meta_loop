# Meta Loop Phase 7–8 Implementation Plan

**Goal:** Integrate the already-completed Phase 6 baseline by verification only, harden and fast-forward-integrate Phase 7, then implement Phase 8 PR publication and CI integration on its own branch without real GitHub writes or Phase 9 behavior.

**Architecture:** Phase 7 remains GitHub REST read-only intake with a canonical Issue spec in CAS and transactional PostgreSQL ledger. Phase 8 turns one registered `text/x-diff` ArtifactRef into a deterministic fixed-SHA candidate, verifies it in a disposable Docker container, records a patch-review approval bound to that exact head SHA, then uses fake-first GitHub REST publication/check adapters with durable intent/reconciliation.

**Tech stack:** Python 3.11, PostgreSQL 16, local SHA-256 CAS, Git, Docker, GitHub REST adapters, pytest.

---

## Baseline facts and non-negotiable gates

- Work only in this repository; do not import, initialize, or modify an external `research_loop` checkout or `governance_root`.
- Preserve `HANDOFF.md`; it is ignored by `.git/info/exclude` and must not be deleted or staged.
- `master@0613617` already contains `08a790d feat: add managed Git worktrees` after the Phase 2–5 baseline `a2527d8`. Phase 6 integration and `docs/reports/PHASE6_INTEGRATION_REPORT.md` already exist; do not repeat them.
- `feat/phase7-phase8@07041a7` contains Phase 7 but is not yet in `master`.
- Stop before checkout, merge, or integration if any unrelated user change remains. The current `.gitignore` edit is user-owned and must never be reverted, staged, or absorbed.
- No remote, push, PR, real GitHub API call, target-repository workflow edit, merge, canary, revert, Issue closure, or Task completion is permitted.

## Phase 7 baseline repair and integration

1. Harden `IssueTaskSpec` parsing: exact schema-v1 object; string objective; list of string acceptance criteria; lowercase 40-hex revision; bounded payloads; no implicit `str()` coercion.
2. Harden GitHub REST intake: validate one explicit `owner/repo`, actor and label; use bounded event pagination; select the latest matching label event; re-fetch the Issue before candidate creation to reject TOCTOU drift; require open non-PR, configured label/actor, and an exact verified commit SHA.
3. Add explicit `github_issue_ingestion` governance authorization and revision. Missing/unreadable/unapproved governance blocks before CAS/Task/queue writes. This does not import `governance_root`; production runtime remains fail-closed when unavailable.
4. Replace process-reset `SequentialIdGenerator("github")` with an infrastructure UUID generator; retain deterministic fakes in tests.
5. Make PostgreSQL source-ledger conflict handling precise, test same-source concurrency and trigger-event conflicts, and document CAS-before-UoW rollback as report-only orphan semantics.
6. Align runtime and `.env.example` on `META_LOOP_POSTGRES_DSN`; emit stable schema-v1 JSON success/error envelopes for every `meta-loop github sync --json` path without disclosing provider/DB exceptions.
7. Run unit, fake-transport, disposable PostgreSQL 16, compileall, diff, boundary, and secret-leak gates. Update Phase 7 spec/report/security review and stale README/bootstrap documents only after green results.
8. With a clean worktree and `master == 0613617`, fast-forward only: `git merge --ff-only feat/phase7-phase8`; then add a standalone Phase 7 integration report and record exact pre/post gates. No merge commit.

## Phase 8 public contracts and persistence

Add schema-v1 DTOs with canonical serialization and no path, command, environment, token, DSN, raw blob, Issue body, or subprocess output:

- `PublicationIntent`: publication/task/worker-result IDs, unified-diff ArtifactRef digest, repository, fixed base SHA, expected versions, correlation and governance revision.
- `PreparedHead`: publication ID, patch digest, base/tree/head SHA, deterministic ref.
- `VerificationResult`: exact head, pinned verifier image digest, profile, controlled result code/status.
- `PublicationReviewDecision`: approval/task/session/result IDs, approval ArtifactRef, base/tree/head SHA, patch-reviewer identity, decision/time.
- `PullRequestReceipt`, `CheckRunObservation`, `PublicationReceipt`, and `CheckGateResult`.

Extend `UnitOfWork` with `PublicationLedger`, `PublicationApprovalStore`, `PublicationEffectStore`, and `CheckRunObservationStore`; implement each in memory and PostgreSQL.

`0007_phase7_issue_ingestion.sql` remains strictly for append-only Issue source ingestion. Add `0008_phase8_publications.sql` only for:

- publication intent/record with immutable canonical input, registered patch digest FK, base/tree/head SHA, deterministic ref, and optimistic state;
- append-only exact-head approvals;
- durable external-effect intent/reconciliation rows;
- append-only exact-head check-run snapshots.

No Phase 8 table stores credentials, blob/diff content, raw API data, paths, commands, environment, or logs.

## Phase 8 flow

1. `prepare` requires R0–R2, `READY_TO_PUBLISH`, fuse off, authorized `prepare_publication`, a successful implementer result, and a Task-attached/cataloged `text/x-diff` ArtifactRef whose CAS digest re-verifies. Any mismatch rejects before external calls.
2. A local infrastructure-only candidate preparer uses an allowlisted repository and exact base SHA in a disposable managed worktree. It rejects absolute/parent/.git paths, binary patches, symlinks, submodules, rename/copy, and mode changes. Fixed metadata produces a deterministic tree/head SHA.
3. A disposable Docker verifier runs fixed configured argv against the exact candidate with a pinned image digest, `--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges`, non-root, resource/timeout/output limits, and no PAT/DSN inheritance. Only structured safe results cross the boundary.
4. `PublicationApprovalService` has no arbitrary reviewer CLI. It validates a successful `PATCH_REVIEWER` session/result and writes one immutable approval that matches task, patch, base/tree/head exactly. Any head change invalidates prior approval.
5. Before any GitHub write, commit a durable `publish_requested` effect intent. Never hold a database transaction across network I/O.
6. The disabled-by-default REST write adapter creates Git objects, deterministic ref, and PR using Git Data APIs. Returned tree/head must match the prepared values. Retries first inspect the deterministic ref/PR and only reconcile identical state; drift fails closed and never force-updates/deletes.
7. The read adapter fetches check runs only for exact head SHA. Explicit configured required checks must all be `completed/success`; missing/pending/neutral/skipped/cancelled/timed_out/action_required/failure block. Record normalized snapshots only.
8. Task remains `READY_TO_PUBLISH`. Phase 9+ behavior is excluded.

## CLI and security boundaries

Provide stable schema-v1 JSON envelopes and fixed exit codes for:

- `meta-loop github sync --limit N --json`
- `meta-loop publication prepare --publication-id ID --task-id ID --worker-result-id ID --patch-digest HEX --json`
- `meta-loop publication status --publication-id ID --json`
- `meta-loop github publish --publication-id ID --json`
- `meta-loop github checks --publication-id ID --json`

There is deliberately no CLI that can forge an approval. PAT remains private to GitHub HTTP adapters; DSN remains private to PostgreSQL runtime; blobs remain CAS-only; Issue bodies remain in the read adapter/parser; paths/argv/subprocess stay in local Git/Docker infrastructure. Errors are controlled codes/messages and never expose those values.

Critical blockers: any real GitHub write in acceptance; non-CAS diff; base/head drift accepted; bypassed approval/Docker/governance/fuse; secret/path/command leakage; workflow modification; or any Phase 9 behavior. High blockers: no durable effect intent/reconciliation; repeat/drift creating another ref/PR; DB transaction across network; stale check acceptance; unpinned/networked/privileged Docker; mutable canonical ledger.

## Verification

- `python -m pytest -q -rs`
- Fresh disposable PostgreSQL 16 gate with zero failures/skips.
- Actual disposable Docker verifier gate; remove only task-created containers/volumes.
- `python -m compileall -q src tests`
- `git diff --check`
- boundary/import, secret-leak, network, subprocess, and Phase 9 capability scans.
- Fake adapters cover malformed GitHub responses, source drift, concurrent intake/publish, every crash window after remote effect, stale/wrong reviewer head, corrupt CAS, unsafe patch, Docker failure, and every check-run non-success state.
- A Phase 7/8 security review must have zero unresolved Critical/High findings before reporting completion.

## Commit order

Existing commits remain unchanged:

1. `08a790d feat: add managed Git worktrees`
2. `0613617 docs: record Phase 6 integration`
3. `07041a7 feat: add GitHub issue ingestion`

Then:

4. `fix: harden GitHub issue ingestion`
5. `docs: finalize Phase 7 baseline`
6. `git merge --ff-only feat/phase7-phase8` (no commit)
7. `docs: record Phase 7 integration`
8. `docs: specify Phase 8 publication and CI boundary`
9. `feat: add publication domain and ledger`
10. `feat: add deterministic patch preparation`
11. `feat: add disposable Docker verification`
12. `feat: add GitHub PR publication recovery`
13. `feat: add check run reconciliation and CLI`
14. `docs: record Phase 8 completion`

Stop after Phase 8 completion. A Phase 8 fast-forward into `master` requires separate authorization.
