CREATE TABLE publications (
    publication_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    worker_result_id TEXT NOT NULL REFERENCES worker_results(result_id),
    patch_digest TEXT NOT NULL REFERENCES artifact_catalog(digest) CHECK (patch_digest ~ '^[0-9a-f]{64}$'),
    repository_name TEXT NOT NULL CHECK (repository_name ~ '^[^/]+/[^/]+$'),
    base_sha TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    tree_sha TEXT NULL CHECK (tree_sha IS NULL OR tree_sha ~ '^[0-9a-f]{40}$'),
    head_sha TEXT NULL CHECK (head_sha IS NULL OR head_sha ~ '^[0-9a-f]{40}$'),
    deterministic_ref TEXT NULL CHECK (deterministic_ref IS NULL OR deterministic_ref = 'refs/meta-loop/' || publication_id),
    canonical_input JSONB NOT NULL CHECK (canonical_input ->> 'schema_version' = '1'),
    verification_json JSONB NULL CHECK (verification_json IS NULL OR verification_json ->> 'schema_version' = '1'),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'prepared', 'verified', 'publish_requested', 'published')),
    version INTEGER NOT NULL CHECK (version >= 0),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_publication_input_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.canonical_input IS DISTINCT FROM OLD.canonical_input
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.worker_result_id IS DISTINCT FROM OLD.worker_result_id
        OR NEW.patch_digest IS DISTINCT FROM OLD.patch_digest
        OR NEW.repository_name IS DISTINCT FROM OLD.repository_name
        OR NEW.base_sha IS DISTINCT FROM OLD.base_sha THEN
        RAISE EXCEPTION 'publication canonical input is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER publications_input_immutable
BEFORE UPDATE ON publications
FOR EACH ROW EXECUTE FUNCTION reject_publication_input_mutation();

CREATE TABLE publication_approvals (
    approval_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    session_id TEXT NOT NULL REFERENCES runner_sessions(session_id),
    result_id TEXT NOT NULL REFERENCES worker_results(result_id),
    approval_artifact_digest TEXT NOT NULL REFERENCES artifact_catalog(digest) CHECK (approval_artifact_digest ~ '^[0-9a-f]{64}$'),
    patch_digest TEXT NOT NULL CHECK (patch_digest ~ '^[0-9a-f]{64}$'),
    base_sha TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    tree_sha TEXT NOT NULL CHECK (tree_sha ~ '^[0-9a-f]{40}$'),
    head_sha TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at TIMESTAMPTZ NOT NULL,
    decision_json JSONB NOT NULL CHECK (decision_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_publication_approval_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'publication approvals are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER publication_approvals_append_only
BEFORE UPDATE OR DELETE ON publication_approvals
FOR EACH ROW EXECUTE FUNCTION reject_publication_approval_mutation();

CREATE TABLE publication_effects (
    effect_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    head_sha TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    deterministic_ref TEXT NOT NULL CHECK (deterministic_ref = 'refs/meta-loop/' || publication_id),
    intent_json JSONB NOT NULL CHECK (intent_json ->> 'schema_version' = '1'),
    state TEXT NOT NULL CHECK (state IN ('requested', 'reconciled')),
    receipt_json JSONB NULL CHECK (receipt_json IS NULL OR receipt_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reconciled_at TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX publication_effects_publication_ref_unique
    ON publication_effects (publication_id, deterministic_ref);

CREATE OR REPLACE FUNCTION reject_publication_effect_intent_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.publication_id IS DISTINCT FROM OLD.publication_id
        OR NEW.head_sha IS DISTINCT FROM OLD.head_sha
        OR NEW.deterministic_ref IS DISTINCT FROM OLD.deterministic_ref
        OR NEW.intent_json IS DISTINCT FROM OLD.intent_json THEN
        RAISE EXCEPTION 'publication effect intent is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER publication_effects_intent_immutable
BEFORE UPDATE ON publication_effects
FOR EACH ROW EXECUTE FUNCTION reject_publication_effect_intent_mutation();

CREATE TABLE check_run_observations (
    observation_id BIGSERIAL PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    head_sha TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    check_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed')),
    conclusion TEXT NULL CHECK (conclusion IS NULL OR conclusion IN ('success', 'failure', 'neutral', 'skipped', 'cancelled', 'timed_out', 'action_required')),
    observed_at TIMESTAMPTZ NOT NULL,
    observation_json JSONB NOT NULL CHECK (observation_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (publication_id, head_sha, check_name, observed_at),
    CHECK ((status = 'completed' AND conclusion IS NOT NULL) OR (status IN ('queued', 'in_progress') AND conclusion IS NULL))
);

CREATE OR REPLACE FUNCTION reject_check_run_observation_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'check run observations are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER check_run_observations_append_only
BEFORE UPDATE OR DELETE ON check_run_observations
FOR EACH ROW EXECUTE FUNCTION reject_check_run_observation_mutation();
