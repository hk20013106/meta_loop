CREATE TABLE source_ingestions (
    source_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    trigger_event_id TEXT NOT NULL UNIQUE,
    canonical_request JSONB NOT NULL CHECK (canonical_request ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_source_ingestion_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'source ingestions are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER source_ingestions_immutable
BEFORE UPDATE OR DELETE ON source_ingestions
FOR EACH ROW EXECUTE FUNCTION reject_source_ingestion_mutation();
