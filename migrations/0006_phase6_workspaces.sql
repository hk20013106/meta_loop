CREATE TABLE workspace_allocations (
    workspace_key BIGSERIAL PRIMARY KEY,
    allocation_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    repository_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('planner', 'design_reviewer', 'implementer', 'patch_reviewer')),
    purpose TEXT NOT NULL CHECK (purpose IN ('planning', 'review', 'proposal', 'implementation')),
    source_revision TEXT NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40,}$'),
    request_json JSONB NOT NULL CHECK (request_json ->> 'schema_version' = '1'),
    read_only BOOLEAN NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (allocation_id)
);
CREATE UNIQUE INDEX workspace_allocations_active_task_purpose_idx
    ON workspace_allocations (task_id, purpose) WHERE state = 'active';
