CREATE TABLE runner_sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role TEXT NOT NULL CHECK (role IN ('planner', 'design_reviewer', 'implementer', 'patch_reviewer')),
    expected_task_version INTEGER NOT NULL CHECK (expected_task_version >= 0),
    expected_sequence INTEGER NOT NULL CHECK (expected_sequence >= 0),
    request_json JSONB NOT NULL CHECK (request_json ->> 'schema_version' = '1'),
    state TEXT NOT NULL CHECK (state IN ('requested', 'succeeded', 'failed', 'cancelled')),
    outcome_json JSONB NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX runner_sessions_task_idx ON runner_sessions (task_id, created_at);
