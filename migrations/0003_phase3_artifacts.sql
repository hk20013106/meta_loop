CREATE TABLE artifact_catalog (
    digest TEXT PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
    logical_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    classification TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
