# Phase 3 specification: local content-addressed artifacts

Phase 3 provides a local SHA-256 CAS and no remote artifact service.
`FilesystemArtifactStore` owns streaming blob writes, integrity verification,
deduplication, reads and read-only orphan scans. Objects live at
`<root>/sha256/ab/cd/<digest>` and are written through a same-filesystem
temporary file with flush, fsync and atomic replace.

`ArtifactCatalog` owns PostgreSQL metadata. `ArtifactRegistrationService`
writes the verified blob first, then uses one UoW to register catalog metadata,
append an `ARTIFACT_REGISTERED` event containing only `ArtifactRef`, and attach
that reference to the Task. A failed database transaction can leave an orphan;
scan reports it and never deletes it.

Requests require logical name, media type, classification and source kind.
`contains_secrets=True` is rejected before any blob write. The CAS root must
not be the source repository or a descendant; unsafe paths, symlink objects,
digest mismatch and detected corruption fail closed. POSIX roots/objects use
0700/0600; Windows reports its ACL limitation rather than claiming POSIX-mode
enforcement.

Non-goals: remote CAS, encryption/key management, artifact deletion, worker
execution, GitHub/Hermes integration, and Phase 4 behavior.
