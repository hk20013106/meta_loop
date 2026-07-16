# Phase 7 specification: GitHub Issue ingestion

Phase 7 reconciles a single configured GitHub repository through REST. It does
not host a webhook endpoint. Only an open Issue carrying the configured label,
whose matching label event was emitted by the configured trigger authority, is
eligible for ingestion.

The Issue body contains exactly one fenced `meta-loop-json` schema-v1 object:
`objective`, `acceptance_criteria`, full 40-character `revision`, and `risk`.
Unknown fields, credential-like values, non-pinned revisions, pull requests and
unverified commit responses are rejected. Provider identity, issue identity,
event identity, actor and time always come from GitHub REST responses.

The canonical specification is written to CAS as
`application/vnd.meta-loop.issue-task+json`. The source ledger, Task, received
event, artifact registration event, source event and queue row share a UoW.
The ledger is append-only and one source Issue maps to one Task; changed source
content produces an idempotency conflict rather than silently modifying work.

The `meta-loop github sync` command reports stable schema-v1 results. PATs are
read only by the HTTP adapter, are never serialized, and are not passed to
workers, runners, workspaces or application DTOs.

Non-goals: webhook hosting, GitHub write APIs, pull requests, CI, merge,
automatic issue closure, real worker execution and Phase 8 behavior.
