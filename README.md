# Meta Loop — Phase 0–7 Baseline

This directory is the independent `meta_loop` system through **Phase 7**.
It contains the Phase 0 boundary, the Phase 1 domain/persistence baseline,
the local controller, CAS, fake runner/worker protocols, managed Git worktree
boundary, and GitHub REST Issue ingestion. Phase 8+ publication runtime
behavior remains out of scope.

## Boundary model

```
research_loop/      # existing auditable scientific research DAG (untouched by Phase 0)
meta_loop/          # NEW independent system (this directory)
governance_root/    # separate governance location (NOT created in Phase 0; see Unresolved)
```

- `meta_loop` must never import `research_loop` internals. The two systems
  share no runtime dependency; they are coordinated only through explicit,
  schema-validated artifacts and GitHub events.
- `governance_root` is a distinct boundary (trigger authority, review policy,
  fuse/rollback). Its concrete location is an open decision (see Unresolved).
- Configuration secrets are never committed. Use `.env.example` as the
  contract for environment-specific, non-secret values.

## Delivered baseline

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
8. Boundary and disposable-database tests that prove:
   - `meta_loop` imports without pulling in `research_loop`;
   - `meta_loop` is a sibling directory, not a child of `research_loop`;
   - `.env.example` contains no real secrets;
   - no GitHub write, PR, CI, merge or production runtime is present.

## Excluded capabilities (Phase 8+)

No GitHub PR publication, CI integration, auto-merge, governance-policy
authoring, real Hermes execution, or production deployment is present. Phase 7
does not host webhooks or contact real GitHub during acceptance.

## Configuration boundaries

- **GitHub repo / trigger actor / trigger label**: explicit local configuration
  is required for Phase 7 reconciliation. The PAT is read only by the HTTP
  infrastructure adapter and is never serialized.
- **PostgreSQL DSN**: placeholder only; no real credentials.
- **governance_root location**: separate boundary, not created in Phase 0.
