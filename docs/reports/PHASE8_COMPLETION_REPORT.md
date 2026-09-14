# Phase 8 completion report

Status: `COMPLETE` for the Phase 8 acceptance boundary defined in `docs/phases/PHASE8_SPEC.md`.

Phase 8 adds an opt-in, fail-closed publication path from one canonical Task-attached `text/x-diff` ArtifactRef to an exact prepared Git head, isolated Docker verification, exact-head reviewer approval, durable GitHub publication intent/reconciliation, exact-head required-check evaluation, and stable CLI surfaces. Phase 9 behavior remains out of scope.

## Implemented scope

1. **Canonical publication preparation**
   - `PublicationPreparationService` accepts only the registered CAS patch identity and validates task/result/session/version/governance/fuse/risk constraints.
   - `LocalGitCandidatePreparer` re-checks patch SHA-256 bytes, rejects unsafe patch forms and derives deterministic `base_sha`, `tree_sha`, `head_sha`, and `refs/heads/meta-loop/<publication_id>` without creating a local publication ref.

2. **Disposable verifier**
   - `DockerCandidateVerifier` verifies only the exact prepared head.
   - The verifier image must be pinned by `sha256` and the container runs with network disabled, read-only filesystem, dropped capabilities, `no-new-privileges`, non-root identity, resource limits, timeout, and read-only workspace mount.
   - The acceptance suite includes an actual Docker-backed verification test against a pinned Alpine image.

3. **Exact-head approval**
   - `PublicationApprovalService` derives approval only from a stored successful `PATCH_REVIEWER` session/result and binds patch/base/tree/head identity.
   - There is deliberately no direct CLI `approve` command.

4. **Durable publication effect and recovery**
   - A publication effect intent is committed before GitHub I/O.
   - Retry/recovery reconciles only the same deterministic ref/PR and exact prepared head; drift or ambiguity fails closed.
   - Network I/O is outside database transactions.

5. **GitHub publisher**
   - `GitHubPublicationPublisher` is disabled by default and requires `META_LOOP_GITHUB_WRITE_ENABLED=1` for runtime writes.
   - It uploads exact Git blobs/tree/commit and rejects any remote SHA mismatch before creating/reconciling the deterministic branch and pull request.
   - It contains no force-update, delete, merge, auto-merge, close, or branch-cleanup behavior.
   - Phase 8 acceptance uses injected fake transports; no real GitHub write is required or counted as acceptance evidence.

6. **Exact-head check gate**
   - `PublicationCheckService` reads current required checks for the exact published head and persists normalized observations.
   - Missing, pending, failed, stale, malformed, or wrong-head checks fail closed; historical success does not override the current snapshot.

7. **CLI**
   - `meta-loop publication prepare ... --json`
   - `meta-loop publication status ... --json`
   - `meta-loop github publish ... --json`
   - `meta-loop github checks ... --json`
   - Stable schema-v1 success/error envelopes are retained and error text is redacted.

8. **Persistence**
   - Existing migration `0008_phase8_publications.sql` remains immutable.
   - Additive migration `0009_phase8_github_branch_refs.sql` repairs the deterministic GitHub branch-ref shape while preserving publication-effect immutability semantics.
   - Memory and PostgreSQL adapters continue to share the same publication conflict/idempotency contracts.

## Acceptance evidence

The implementation was exercised on GitHub Actions against disposable PostgreSQL 16. The latest pre-documentation acceptance run for the implementation branch passed the full suite with `215 passed, 0 failed`, and `python -m compileall -q src tests` passed. The suite includes the real Docker verifier acceptance path as well as PostgreSQL publication-ledger, approval, publish-recovery, exact-head checks and CLI coverage.

The final documentation HEAD must also remain green under the same repository CI before this report is treated as the branch-closing evidence.

## Security gate

`docs/reports/PHASE8_SECURITY_REVIEW.md` records:

- Critical: 0 unresolved
- High: 0 unresolved
- Medium: 0 unresolved security blockers

The review explicitly covers CAS-only publication identity, exact Git object SHA preservation, Docker isolation, approval binding, retry/recovery, current-check evaluation, PAT/path/DSN redaction, transaction boundaries, migration immutability, fail-closed governance, and absence of Phase 9 capabilities.

The review also found and corrected a CLI-composition regression before closure: `doctor` had accidentally become migration-applying and CAS had gained an implicit default path. Both were restored to the established read-only/fail-closed configuration semantics.

## Non-goals and remaining operational boundaries

Phase 8 does **not** implement automated merge, branch deletion, rollback/canary, Issue closure, Task completion, webhook hosting, deployment runtime, governance-policy authoring, or any Phase 9 behavior.

Production governance remains an external boundary and is intentionally unavailable in the current composition; authorization-dependent commands therefore fail closed until that boundary is supplied.

Real GitHub write E2E is intentionally excluded from Phase 8 acceptance. Runtime publication remains explicitly opt-in and disabled by default.

## Branch state at closure

Phase 8 work remains on `codex/phase8-publisher-ci`. It is not merged into `main` by this report. Merge/integration is a separate owner-authorized operation.
