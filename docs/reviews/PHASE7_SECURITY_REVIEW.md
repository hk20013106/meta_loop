# Phase 7 security review

Critical: none. High: none.

Resolved controls: fixed GitHub API origin; PAT confined to an HTTP adapter;
strict schema-v1 fenced Issue contract with duplicate-key/type/bounds checks;
source identity and trigger actor read from GitHub API responses; bounded event
pagination and latest-event selection; Issue re-fetch/snapshot drift rejection;
PR/closed Issue rejection; full SHA verification; credential-like input
rejection; append-only locked source ledger; CAS references rather than raw
Issue body in Task events; fuse, explicit ingestion governance and UoW rollback
protection; UUID runtime IDs; and controlled CLI error envelopes that exclude
PATs, DSNs, blobs, raw Issue text, paths, commands and provider/database errors.

Residual Medium: REST reconciliation is not a signed webhook protocol. This is
intentional for the local Controller deployment; an unavailable or malformed
provider response fails closed and creates no Task. CAS writes precede the UoW,
so an aborted transaction may leave a report-only orphan blob; it is not
cataloged or task-visible.

Not implemented: GitHub write APIs, PR publication, CI, merge, close/reopen
operations, webhook handling, real Hermes, or research execution.
