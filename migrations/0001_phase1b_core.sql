CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('received', 'validated', 'planned', 'design_review', 'implementing', 'preflight', 'patch_review', 'ready_to_publish', 'proposal_ready', 'rework_required', 'blocked', 'failed', 'completed')),
    version INTEGER NOT NULL CHECK (version >= 0),
    effective_risk TEXT NOT NULL CHECK (effective_risk IN ('R0', 'R1', 'R2', 'R3')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_json JSONB NOT NULL CHECK (task_json ->> 'schema_version' = '1')
);

CREATE TABLE task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    actor TEXT NOT NULL,
    payload JSONB NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    causation_id TEXT NULL,
    correlation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    UNIQUE (task_id, sequence)
);

CREATE FUNCTION reject_task_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'task_events are append-only'; END;
$$;
CREATE TRIGGER task_events_append_only BEFORE UPDATE OR DELETE ON task_events
FOR EACH ROW EXECUTE FUNCTION reject_task_event_mutation();

CREATE TABLE task_queue (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    state TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'leased')),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT NULL,
    claimed_at TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    last_error TEXT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state = 'ready' AND claimed_by IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR (state = 'leased' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX task_queue_claimable_idx ON task_queue (priority DESC, available_at ASC) WHERE state = 'ready';

-- The application role must not receive TRUNCATE on task_events.
-- Claim transactions use SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1.
