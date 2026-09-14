CREATE TABLE integrations (
    integration_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    repository_name TEXT NOT NULL CHECK (repository_name ~ '^[^/]+/[^/]+$'),
    target_branch TEXT NOT NULL,
    pull_request_number INTEGER NOT NULL CHECK (pull_request_number > 0),
    base_sha TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    base_tree_sha TEXT NOT NULL CHECK (base_tree_sha ~ '^[0-9a-f]{40}$'),
    approved_head_sha TEXT NOT NULL CHECK (approved_head_sha ~ '^[0-9a-f]{40}$'),
    prepared_tree_sha TEXT NOT NULL CHECK (prepared_tree_sha ~ '^[0-9a-f]{40}$'),
    deterministic_ref TEXT NOT NULL,
    governance_revision TEXT NOT NULL,
    expected_task_version INTEGER NOT NULL CHECK (expected_task_version >= 0),
    expected_sequence INTEGER NOT NULL CHECK (expected_sequence >= 0),
    canonical_input JSONB NOT NULL CHECK (canonical_input ->> 'schema_version' = '1'),
    state TEXT NOT NULL CHECK (state IN (
        'merge_requested', 'merged', 'canary_pending', 'canary_passed',
        'canary_failed', 'revert_requested', 'revert_published',
        'revert_checked', 'revert_merged', 'manual_intervention_required'
    )),
    version INTEGER NOT NULL CHECK (version >= 0),
    merge_receipt_json JSONB NULL CHECK (merge_receipt_json IS NULL OR merge_receipt_json ->> 'schema_version' = '1'),
    canary_json JSONB NULL CHECK (canary_json IS NULL OR canary_json ->> 'schema_version' = '1'),
    revert_candidate_json JSONB NULL CHECK (revert_candidate_json IS NULL OR revert_candidate_json ->> 'schema_version' = '1'),
    manual_reason TEXT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (deterministic_ref = 'refs/heads/meta-loop/' || publication_id)
);

CREATE UNIQUE INDEX integrations_one_unresolved_target
    ON integrations (repository_name, target_branch)
    WHERE state NOT IN ('canary_passed', 'revert_merged');

CREATE OR REPLACE FUNCTION reject_integration_input_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.canonical_input IS DISTINCT FROM OLD.canonical_input
        OR NEW.publication_id IS DISTINCT FROM OLD.publication_id
        OR NEW.effect_id IS DISTINCT FROM OLD.effect_id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.repository_name IS DISTINCT FROM OLD.repository_name
        OR NEW.target_branch IS DISTINCT FROM OLD.target_branch
        OR NEW.pull_request_number IS DISTINCT FROM OLD.pull_request_number
        OR NEW.base_sha IS DISTINCT FROM OLD.base_sha
        OR NEW.base_tree_sha IS DISTINCT FROM OLD.base_tree_sha
        OR NEW.approved_head_sha IS DISTINCT FROM OLD.approved_head_sha
        OR NEW.prepared_tree_sha IS DISTINCT FROM OLD.prepared_tree_sha
        OR NEW.deterministic_ref IS DISTINCT FROM OLD.deterministic_ref
        OR NEW.governance_revision IS DISTINCT FROM OLD.governance_revision
        OR NEW.expected_task_version IS DISTINCT FROM OLD.expected_task_version
        OR NEW.expected_sequence IS DISTINCT FROM OLD.expected_sequence THEN
        RAISE EXCEPTION 'integration canonical input is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER integrations_input_immutable
BEFORE UPDATE ON integrations
FOR EACH ROW EXECUTE FUNCTION reject_integration_input_mutation();

CREATE TABLE integration_effects (
    integration_effect_id TEXT PRIMARY KEY,
    integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
    effect_kind TEXT NOT NULL CHECK (effect_kind IN ('merge', 'revert_publish', 'revert_merge')),
    intent_json JSONB NOT NULL CHECK (intent_json ->> 'schema_version' = '1'),
    state TEXT NOT NULL CHECK (state IN ('requested', 'reconciled')),
    receipt_json JSONB NULL CHECK (receipt_json IS NULL OR receipt_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reconciled_at TIMESTAMPTZ NULL,
    UNIQUE (integration_id, effect_kind)
);

CREATE OR REPLACE FUNCTION reject_integration_effect_intent_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.integration_id IS DISTINCT FROM OLD.integration_id
        OR NEW.effect_kind IS DISTINCT FROM OLD.effect_kind
        OR NEW.intent_json IS DISTINCT FROM OLD.intent_json THEN
        RAISE EXCEPTION 'integration effect intent is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER integration_effects_intent_immutable
BEFORE UPDATE ON integration_effects
FOR EACH ROW EXECUTE FUNCTION reject_integration_effect_intent_mutation();

CREATE TABLE integration_check_observations (
    observation_id BIGSERIAL PRIMARY KEY,
    integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('merge', 'revert')),
    subject_sha TEXT NOT NULL CHECK (subject_sha ~ '^[0-9a-f]{40}$'),
    check_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed')),
    conclusion TEXT NULL CHECK (conclusion IS NULL OR conclusion IN ('success', 'failure', 'neutral', 'skipped', 'cancelled', 'timed_out', 'action_required')),
    observed_at TIMESTAMPTZ NOT NULL,
    observation_json JSONB NOT NULL CHECK (observation_json ->> 'schema_version' = '1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (integration_id, subject_kind, subject_sha, check_name, observed_at),
    CHECK ((status = 'completed' AND conclusion IS NOT NULL) OR (status IN ('queued', 'in_progress') AND conclusion IS NULL))
);

CREATE OR REPLACE FUNCTION reject_integration_check_observation_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'integration check observations are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER integration_check_observations_append_only
BEFORE UPDATE OR DELETE ON integration_check_observations
FOR EACH ROW EXECUTE FUNCTION reject_integration_check_observation_mutation();
