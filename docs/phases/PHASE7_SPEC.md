# Phase 7 specification: GitHub Issue ingestion

Phase 7 reconciles a single configured GitHub repository through REST. It does
not host a webhook endpoint. Only an open Issue carrying the configured label,
whose matching label event was emitted by the configured trigger authority, is
eligible for ingestion.

The Issue body contains exactly one fenced `meta-loop-json` schema-v1 object:
`objective`, `acceptance_criteria`, full lowercase 40-character `revision`,
and `risk`. The parser rejects duplicate keys, unknown fields, non-string
values, unbounded payloads, credential-like values and non-pinned revisions.
Provider identity, issue identity, event identity, actor and time always come
from bounded GitHub REST responses.

The REST adapter uses explicit `owner/repo`, trigger actor and trigger label
configuration. It selects the latest matching label event using parsed UTC
time and numeric event ID, then re-fetches the Issue before creating a
candidate. It rejects any snapshot/refetch drift, closed Issue, Pull Request,
missing actor/label or unverified commit response before CAS or UoW writes.

The canonical specification is written to CAS as
`application/vnd.meta-loop.issue-task+json`. The source ledger, Task, received
event, artifact registration event, source event and queue row share a UoW.
The ledger is append-only and one source Issue maps to one Task; changed source
content produces an idempotency conflict rather than silently modifying work.
Source and trigger identities are reserved before read/create work (with
PostgreSQL advisory and row locks); an engaged fuse, missing
`github_issue_ingestion` governance authorization/revision, trigger collision
or source drift fails closed. A CAS write that precedes a rejected UoW is a
report-only orphan, never a Task-visible artifact.

The `meta-loop github sync` command reports stable schema-v1 success and error
envelopes. Runtime IDs use UUIDs; PATs are read only by the HTTP infrastructure
adapter, are never serialized, and are not passed to workers, runners,
workspaces or application DTOs. PostgreSQL DSNs, blobs, raw Issue bodies,
provider exceptions and commands are likewise outside DTOs and CLI output.

Non-goals: webhook hosting, GitHub write APIs, pull requests, CI, merge,
automatic issue closure, real worker execution and Phase 8 behavior.
