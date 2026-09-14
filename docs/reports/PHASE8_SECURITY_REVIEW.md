# Phase 8 security review

Status: `PASS`

Reviewed scope: Phase 8 changes from `main` (`dcc897da687be70acce7f4a23bc05e5d1e528d91`) through the feature branch, with emphasis on candidate preparation, Docker verification, reviewer approval, durable publication effects, GitHub publication, exact-head check evaluation, CLI composition, PostgreSQL migration `0009`, credentials, local paths, and transaction boundaries.

## Severity result

- Critical: 0 unresolved
- High: 0 unresolved
- Medium: 0 unresolved security blockers
- Low / operational limitations: documented below

## Security properties verified

1. **Exact identity is preserved.** Candidate patch bytes are SHA-256 checked against the canonical artifact digest. Prepared `base_sha`, `tree_sha`, and `head_sha` are revalidated before verification and publication. GitHub-created blob, tree, and commit SHAs must exactly match the corresponding local Git objects; no downstream semantic re-hashing or equivalent-identity path is accepted.
2. **GitHub publication is fail-closed and opt-in.** The writer is disabled by default and runtime writes require `META_LOOP_GITHUB_WRITE_ENABLED=1`. The adapter exposes GET/POST only; Phase 8 contains no force-update, ref-delete, branch-delete, merge, or auto-merge operation.
3. **Retry does not mutate drifted remote state.** Existing publication refs and pull requests are reconciled only when their exact base/head refs and SHAs match the durable publication. Ref/PR drift, ambiguous PR state, or object-SHA mismatch is rejected.
4. **Reviewer approval is bound to the exact verified head.** Approval is derived from a successful canonical `PATCH_REVIEWER` session/result and append-only approval artifact, then records patch/base/tree/head identity. There is no CLI command that lets a caller directly synthesize an approval.
5. **Remote I/O is outside database transactions.** Publish intent is durably committed before GitHub network I/O and the receipt is reconciled in a second transaction. Exact-head check reads likewise occur outside the UoW and are persisted only after state is revalidated.
6. **Current checks, not stale history, determine the gate.** Required checks must be present on the current exact head and currently be `completed/success`; missing, pending, failing, malformed, or wrong-head observations fail closed.
7. **Verifier isolation is restrictive.** Docker verification requires an image pinned by `sha256` and uses `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, a non-root user, PID/memory/CPU limits, timeout, and a read-only workspace mount. Host secrets are not forwarded to the container.
8. **Patch/workspace boundaries are constrained.** Binary patches, symlinks, submodules, renames/copies/mode changes and unsafe paths are rejected. Local workspaces use configured repository allowlists, fixed commit SHAs, managed-root containment checks, and symlink checks.
9. **Credentials and local paths are not serialized into domain DTOs or CLI errors.** GitHub PAT is kept in the HTTP Authorization header only. CLI error envelopes use fixed redacted messages and tests cover PAT/path/DSN-shaped exception text.
10. **Production governance remains fail-closed.** No allow-all governance implementation was introduced. The current production composition reports governance unavailable and preparation/ingestion cannot bypass authorization.
11. **Migration history remains immutable.** `0008` was not edited. `0009` performs the additive legacy-ref migration and restores the publication-effect immutability trigger inside the migration transaction.
12. **No Phase 9 capability was added.** There is no automated merge, branch deletion, rollback publisher, webhook server, deployment runtime, or policy-authoring path in this phase.

## Verification evidence

GitHub Actions ran the full suite against disposable PostgreSQL 16 and reported `215 passed` with no skips/failures. `python -m compileall -q src tests` also passed. GitHub publication acceptance is fake-first: no real Phase 8 branch or pull request was created during tests.

The review also caught one unrelated runtime regression before completion: the Phase 8 CLI composition had changed `doctor` from migration validation to migration application and had introduced an implicit CAS default. Both were reverted to the established fail-closed/read-only semantics before this review was closed.

A separate external static analyzer was not counted as evidence for this review; the result above is based on code/diff inspection plus the repository's executable tests and PostgreSQL integration gate.

## Non-blocking operational limitations

- The concrete production governance reader remains external/unavailable, so authorization-dependent runtime commands fail closed until that boundary is supplied.
- Publication candidate commits are intentionally not made reachable by a local Git ref during preparation. If an operator runs aggressive Git garbage collection before publication, the later publication step can fail unavailable; it fails closed rather than publishing a different object.
- Real GitHub write E2E is intentionally excluded from Phase 8 acceptance. The writer is exercised with injected fake transports and exact-object contracts, and remains disabled by default in runtime configuration.
