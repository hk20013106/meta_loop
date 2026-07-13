CREATE TABLE system_fuse (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    engaged BOOLEAN NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    governance_revision TEXT NULL
);
INSERT INTO system_fuse (singleton, engaged, changed_at) VALUES (TRUE, FALSE, now());

CREATE TABLE intake_ledger (
    request_id TEXT PRIMARY KEY,
    request_json JSONB NOT NULL CHECK (request_json ->> 'schema_version' = '1'),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE control_events (
    sequence BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('fuse_engaged', 'fuse_released')),
    occurred_at TIMESTAMPTZ NOT NULL,
    governance_revision TEXT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);
CREATE FUNCTION reject_control_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'control_events are append-only'; END;
$$;
CREATE TRIGGER control_events_append_only BEFORE UPDATE OR DELETE ON control_events
FOR EACH ROW EXECUTE FUNCTION reject_control_event_mutation();
