# Meta Loop — Phase 0–1B Baseline

This directory is the independent `meta_loop` system through **Phase 1B**.
It contains the Phase 0 boundary, Phase 1A domain model and ports, and Phase
1B PostgreSQL persistence/queue adapter. Phase 2+ runtime behavior remains
out of scope.

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
4. A `.env.example` with explicit placeholders only (no real PAT / DSN / key).
5. Boundary and disposable-database tests that prove:
   - `meta_loop` imports without pulling in `research_loop`;
   - `meta_loop` is a sibling directory, not a child of `research_loop`;
   - `.env.example` contains no real secrets;
   - no later-phase runtime symbols are present.

## Excluded capabilities (Phase 2+)

No controller/CLI, CAS, Hermes session protocol, GitHub Issue/PR automation,
auto-merge, governance-policy authoring, or production deployment is present.

## Configuration boundaries

- **GitHub org / repo / identity**: placeholders only. No GitHub runtime is
  implemented in this baseline.
- **PostgreSQL DSN**: placeholder only; no real credentials.
- **governance_root location**: separate boundary, not created in Phase 0.
