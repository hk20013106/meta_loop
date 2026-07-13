"""Phase 0 boundary test: proves meta_loop is an independent, auditable
bootstrap scaffold and does NOT couple to research_loop internals.

This test exercises the real package boundary (import isolation, directory
placement, placeholder-only config) rather than mocking internals.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]  # D:/research_loop
META_LOOP_DIR = REPO_ROOT / "meta_loop"
RESEARCH_LOOP_DIR = REPO_ROOT / "research_loop"
PACKAGE_INIT = META_LOOP_DIR / "src" / "meta_loop" / "__init__.py"


def test_meta_loop_package_present_and_versioned():
    import meta_loop

    assert meta_loop.__version__ == "0.0.0"
    assert meta_loop.__phase__ == "0"


def test_meta_loop_imports_without_research_loop_coupling():
    # Importing meta_loop must not pull research_loop into the process.
    import sys

    import meta_loop  # noqa: F401

    assert "research_loop" not in sys.modules, (
        "meta_loop must not import research_loop internals"
    )


def test_meta_loop_is_separate_from_research_loop_dir():
    # meta_loop must be a sibling of research_loop, never a child of it.
    assert META_LOOP_DIR.exists()
    assert RESEARCH_LOOP_DIR.exists()
    assert META_LOOP_DIR.parent == RESEARCH_LOOP_DIR.parent == REPO_ROOT
    assert not str(META_LOOP_DIR).startswith(str(RESEARCH_LOOP_DIR))


def test_env_example_has_no_real_secrets():
    env_example = META_LOOP_DIR / ".env.example"
    assert env_example.exists()
    text = env_example.read_text(encoding="utf-8")
    # No assignment may contain a real-looking GitHub PAT.
    assert "ghp_" not in text and "github_pat_" not in text
    # Placeholders must be present so later phases have a contract to fill.
    assert "META_LOOP_GITHUB_PAT=" in text
    assert "META_LOOP_TRIGGER_AUTHOR=Kai" in text


def test_init_source_has_no_research_loop_imports():
    text = PACKAGE_INIT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+research_loop", text, re.M), (
        "meta_loop/__init__.py must not import research_loop"
    )


def test_no_later_phase_runtime_present():
    # Phase 0 must not implement later-phase behavior. Proof: the package
    # initializer contains no runtime code (no imports, no defs, no classes)
    # beyond its docstring and version/phase dunders.
    text = PACKAGE_INIT.read_text(encoding="utf-8")
    assert re.search(r"^\s*(import|from)\s+\w", text, re.M) is None, (
        "meta_loop/__init__.py must contain no imports (no runtime)"
    )
    assert re.search(r"^\s*(def|class)\s+\w", text, re.M) is None, (
        "meta_loop/__init__.py must define no runtime symbols"
    )
