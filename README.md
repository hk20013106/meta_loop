# Meta Loop (LoopX)

Meta Loop is an independent, fail-closed maintenance-orchestration system. It records work intake, artifacts, workers, workspaces, governance gates, and publication state. It can be cloned and tested on its own and does not require an adjacent `research_loop` checkout.

The accepted Phase 0–8 scope is implemented on this branch. Phase 8 adds deterministic candidate preparation from the canonical patch artifact, isolated verification, exact-head reviewer approval, durable publication reconciliation, exact-head required-check evaluation, and publication CLI surfaces. Phase 9 behavior is not implemented.

## Boundary model

```text
research_loop/      # optional upstream scientific DAG; never imported here
meta_loop/          # this independent orchestration repository
governance_root/    # external trigger/review-policy authority
```

- `meta_loop` never imports `research_loop` internals.
- Coordination occurs only through explicit, schema-validated artifacts and repository events.
- Production governance remains an external boundary and fails closed when unavailable.

## Implemented capabilities

1. Versioned task/event domain with in-memory and PostgreSQL adapters, append-only facts, optimistic versioning, and a durable queue.
2. Local SHA-256 artifact storage plus runner, worker, workspace, and publication ledgers.
3. Read-only issue reconciliation with pinned commit identity and atomic canonical intake.
4. Phase 8 publication state for immutable intent, prepared head, verification, exact-head approval, durable publication effects, and normalized exact-head check observations.
5. Deterministic local candidate preparation from canonical patch bytes with unsafe patch forms rejected.
6. Isolated disposable verification of the exact prepared head.
7. Reviewer approval derived only from a stored successful reviewer result bound to exact patch/base/tree/head identity.
8. Exact-object publication reconciliation with remote drift and SHA mismatch rejected.
9. Required-check evaluation for the current exact head; stale historical success cannot satisfy the gate.
10. Stable schema-v1 publication CLI surfaces, with no direct approval command.
11. PostgreSQL migrations through `0009_phase8_github_branch_refs.sql`, with `0008` left immutable.
12. Disposable PostgreSQL, verifier, boundary, and fake-provider acceptance tests.

## Not implemented

No automated merge, branch deletion, rollback/canary publisher, Issue closure, Task completion, governance-policy authoring, real Hermes execution, webhook hosting, or production deployment is present. These are Phase 9-or-later concerns or external operational boundaries.

## Install and verify

```powershell
python -m pip install -e .[postgres-test]
python -m pytest -q -rs
python -m compileall -q src tests
meta-loop doctor --json
```

PostgreSQL acceptance requires a disposable database. The Phase 8 verifier acceptance also requires its container runtime. `meta-loop doctor --json` validates availability/state and does not apply migrations.

## Phase reports

- `docs/reports/PHASE8_COMPLETION_REPORT.md` — Phase 8 scope and acceptance summary.
- `docs/reports/PHASE8_SECURITY_REVIEW.md` — Phase 8 security review.

## License

There is currently no `LICENSE` file. Repository visibility does not itself grant an open-source reuse license.
