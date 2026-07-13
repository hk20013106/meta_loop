CREATE TABLE worker_results (
    result_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES runner_sessions(session_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    result_json JSONB NOT NULL CHECK (result_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX worker_results_session_idx ON worker_results (session_id);
