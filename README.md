# Meta Loop (LoopX)

Meta Loop is an independent, fail-closed maintenance-orchestration system. It
records work intake, artifacts, workers, workspaces, governance gates and
publication facts. It can be cloned and tested on its own; it does not require
an adjacent `research_loop` checkout.

The current codebase has completed Phase 0–7 capabilities and has **partial
Phase 8 foundations**: schema-v1 publication DTOs, in-memory/PostgreSQL
publication ledgers, and migration `0008`. Phase 8 is not complete: there is
no candidate-preparation service, Docker verifier, GitHub PR publisher,
check-run gate, or publication CLI. Do not treat the ledger as production
publication runtime.

## Boundary model

```
research_loop/      # optional upstream scientific DAG; never imported here
meta_loop/          # this independent orchestration repository
governance_root/    # external trigger/review-policy authority
```

- `meta_loop` must never import `research_loop` internals. The systems share
  no runtime dependency; any coordination is through explicit,
  schema-validated artifacts and GitHub events.
- `governance_root` is a distinct boundary (trigger authority, review policy,
  fuse/rollback). Its concrete location is an open decision (see Unresolved).
- Configuration secrets are never committed. Use `.env.example` as the
  contract for environment-specific, non-secret values.

## Implemented capabilities

1. An isolated, importable `meta_loop` package (sibling of `research_loop`).
2. A pure, versioned task/event domain with fail-closed governance transitions.
3. In-memory and PostgreSQL UoW adapters, append-only events, optimistic task
   versioning, and a `FOR UPDATE SKIP LOCKED` queue.
4. Controller/CLI intake and fuse controls, a local SHA-256 CAS/catalog, and
   provider-neutral runner, worker and workspace ledgers.
5. An opt-in local Git worktree adapter with fixed-SHA, allowlist and
   read-only/R3 protections; its default contract adapter is deterministic and
   filesystem-free.
6. A read-only GitHub REST Issue reconciler for one explicitly configured
   repository, trigger actor and trigger label. It accepts only verified,
   open non-PR Issues with a pinned commit SHA, writes the canonical Issue
   specification to CAS, and atomically records the source ledger, Task,
   events, ArtifactRef and queue row.
7. A `.env.example` with explicit placeholders only (no real PAT / DSN / key).
8. Phase 8 publication-domain and ledger foundations: immutable publication
   intent, prepared-head, verification, approval, effect and check-observation
   records backed by in-memory and PostgreSQL adapters.
9. Boundary and disposable-database tests that cover:
   - `meta_loop` imports without pulling in `research_loop`;
   - the repository is usable without a `research_loop` checkout;
   - `.env.example` contains no real secrets;
   - existing GitHub integration is read-only Issue intake.

## Not implemented

No candidate preparation, Docker verification, GitHub PR publication,
check-run evaluation, auto-merge, governance-policy authoring, real Hermes
execution, webhook hosting, or production deployment is present. Phase 7 does
not contact real GitHub during acceptance; its runtime adapter is explicitly
configured and read-only.

## Install, test, and run

```powershell
python -m pip install -e .[postgres-test]
python -m pytest -q -rs
python -m compileall -q src tests
meta-loop doctor --json
```

Most unit tests run without services. PostgreSQL integration tests require a
disposable PostgreSQL database via `META_LOOP_TEST_POSTGRES_DSN`; skipped
database tests are not a Phase 8 acceptance result. `meta-loop doctor --json`
is safe to run without configuration and reports which optional local services
are available.

## Repository hygiene and license

The repository is published privately at its configured GitHub `origin`.
`.env` and local agent, TokenSave, build and packaging artifacts are excluded
from version control.
There is currently no `LICENSE` file: the repository is source-available only
until its owner selects and adds a license; no open-source reuse grant is made.

## Configuration boundaries

- **GitHub repo / trigger actor / trigger label**: explicit local configuration
  is required for Phase 7 reconciliation. The PAT is read only by the HTTP
  infrastructure adapter and is never serialized.
- **PostgreSQL DSN**: placeholder only; no real credentials.
- **governance_root location**: separate boundary, not created in Phase 0.
