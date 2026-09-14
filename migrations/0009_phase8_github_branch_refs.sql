-- Phase 8 additive repair: GitHub pull requests require a real refs/heads branch.
-- Do not edit 0008: applied migration checksums are immutable.

ALTER TABLE publications
    DROP CONSTRAINT IF EXISTS publications_deterministic_ref_check;
ALTER TABLE publication_effects
    DROP CONSTRAINT IF EXISTS publication_effects_deterministic_ref_check;

DROP TRIGGER IF EXISTS publication_effects_intent_immutable ON publication_effects;

UPDATE publications
SET deterministic_ref = 'refs/heads/meta-loop/' || publication_id
WHERE deterministic_ref = 'refs/meta-loop/' || publication_id;

UPDATE publication_effects
SET deterministic_ref = 'refs/heads/meta-loop/' || publication_id,
    intent_json = jsonb_set(
        intent_json,
        '{deterministic_ref}',
        to_jsonb('refs/heads/meta-loop/' || publication_id),
        false
    )
WHERE deterministic_ref = 'refs/meta-loop/' || publication_id;

CREATE TRIGGER publication_effects_intent_immutable
BEFORE UPDATE ON publication_effects
FOR EACH ROW EXECUTE FUNCTION reject_publication_effect_intent_mutation();

ALTER TABLE publications
    ADD CONSTRAINT publications_deterministic_ref_check
    CHECK (
        deterministic_ref IS NULL
        OR deterministic_ref = 'refs/heads/meta-loop/' || publication_id
        OR deterministic_ref = 'refs/meta-loop/' || publication_id
    );

ALTER TABLE publication_effects
    ADD CONSTRAINT publication_effects_deterministic_ref_check
    CHECK (
        deterministic_ref = 'refs/heads/meta-loop/' || publication_id
        OR deterministic_ref = 'refs/meta-loop/' || publication_id
    );
