from pathlib import Path

from meta_loop.application.models import PreparedHead, PublicationEffectIntent


def test_phase8_deterministic_ref_is_a_real_github_head_branch():
    ref = "refs/heads/meta-loop/publication-1"
    prepared = PreparedHead("publication-1", "b" * 64, "c" * 40, "d" * 40, "e" * 40, ref)
    effect = PublicationEffectIntent("effect-1", "publication-1", "e" * 40, ref)
    assert prepared.deterministic_ref == ref
    assert effect.deterministic_ref == ref


def test_phase8_branch_ref_upgrade_is_additive_migration():
    migration = Path("migrations/0009_phase8_github_branch_refs.sql")
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    assert "refs/heads/meta-loop/" in sql
    assert "publication_effects_intent_immutable" in sql
    assert "UPDATE publication_effects" in sql
