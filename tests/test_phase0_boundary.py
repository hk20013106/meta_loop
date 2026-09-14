"""Package-boundary tests for an independently clonable ``meta_loop`` repo.

They prove import isolation and placeholder-only configuration without
requiring a separately checked-out ``research_loop`` sibling.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_INIT = REPO_ROOT / "src" / "meta_loop" / "__init__.py"


def test_meta_loop_package_present_and_versioned():
    import meta_loop

    assert meta_loop.__version__ == "0.0.0"


def test_meta_loop_imports_without_research_loop_coupling():
    # Importing meta_loop must not pull research_loop into the process.
    import sys

    import meta_loop  # noqa: F401

    assert "research_loop" not in sys.modules, (
        "meta_loop must not import research_loop internals"
    )


def test_repository_boundary_needs_no_research_loop_checkout():
    # A published Meta Loop clone must remain usable when research_loop is absent.
    assert (REPO_ROOT / "pyproject.toml").exists()
    assert PACKAGE_INIT.exists()
    assert not (REPO_ROOT / "src" / "research_loop").exists()


def test_env_example_has_no_real_secrets():
    env_example = REPO_ROOT / ".env.example"
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
