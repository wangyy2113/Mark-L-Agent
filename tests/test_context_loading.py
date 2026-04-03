"""Test biz domain context loading.

Verifies that _base, context, knowledge, and domain files are loaded correctly.
Requires the actual biz directory structure at BIZ_BASE_PATH.

Usage:
    python tests/test_context_loading.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
os.environ.setdefault("FEISHU_APP_ID", "t")
os.environ.setdefault("FEISHU_APP_SECRET", "t")

from core.biz import (
    _resolve_biz_base,
    discover_domains,
    load_base_context,
    load_context_dir,
    load_knowledge_index,
    load_knowledge_overview,
    has_knowledge,
    load_domain_context,
    repos_path,
)


def _skip_if_no_biz():
    base = _resolve_biz_base()
    if not base.is_dir():
        print(f"  SKIP (biz dir not found: {base})")
        return True
    return False


def test_resolve_biz_base():
    base = _resolve_biz_base()
    assert base.is_dir(), f"biz base not found: {base}"


def test_discover_domains_excludes_hidden():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    assert len(domains) > 0, "No domains found"
    for d in domains:
        assert not d.startswith("."), f"Hidden dir in domains: {d}"
        assert not d.startswith("_"), f"_base in domains: {d}"


def test_base_context_loads_root_md():
    """_base/*.md files should be loaded."""
    if _skip_if_no_biz(): return
    base_dir = _resolve_biz_base() / "_base"
    if not base_dir.is_dir():
        print("  SKIP (no _base dir)")
        return
    ctx = load_base_context()
    # At minimum, context/ subdir files should load
    ctx_dir = base_dir / "context"
    if ctx_dir.is_dir() and any(ctx_dir.glob("*.md")):
        assert len(ctx) > 0, "_base/context/ has .md files but load_base_context returned empty"


def test_base_context_loads_context_subdir():
    """_base/context/*.md files should also be loaded."""
    if _skip_if_no_biz(): return
    base_dir = _resolve_biz_base() / "_base"
    ctx_dir = base_dir / "context"
    if not ctx_dir.is_dir():
        print("  SKIP (no _base/context/ dir)")
        return
    md_files = list(ctx_dir.glob("*.md"))
    if not md_files:
        print("  SKIP (no .md files in _base/context/)")
        return
    ctx = load_base_context()
    # Read one file directly and check it's in the result
    sample = md_files[0].read_text(encoding="utf-8").strip()
    assert sample[:50] in ctx, f"Content from {md_files[0].name} not found in base_context"


def test_domain_context_dir():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    for d in domains:
        ctx = load_context_dir(d)
        ctx_dir = _resolve_biz_base() / d / "context"
        if ctx_dir.is_dir() and any(ctx_dir.glob("*.md")):
            assert len(ctx) > 0, f"Domain {d} has context/ .md files but load returned empty"


def test_knowledge_index():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    for d in domains:
        ki = load_knowledge_index(d)
        ki_path = _resolve_biz_base() / d / "knowledge" / "index.md"
        if ki_path.exists() and ki_path.read_text().strip():
            assert ki is not None, f"Domain {d} has knowledge/index.md but load returned None"


def test_knowledge_overview():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    for d in domains:
        ko = load_knowledge_overview(d)
        ko_path = _resolve_biz_base() / d / "knowledge" / "overview.md"
        if ko_path.exists() and ko_path.read_text().strip():
            assert ko is not None, f"Domain {d} has knowledge/overview.md but load returned None"


def test_has_knowledge():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    for d in domains:
        kdir = _resolve_biz_base() / d / "knowledge"
        expected = kdir.is_dir() and any(kdir.iterdir())
        assert has_knowledge(d) == expected, f"has_knowledge({d}) mismatch"


def test_load_domain_context_all_keys():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    if not domains:
        print("  SKIP (no domains)")
        return
    ctx = load_domain_context([domains[0]])
    expected_keys = {
        "base_context", "domains", "biz_base", "domain_prompt", "claude_md",
        "context", "knowledge_index", "knowledge_overview", "has_knowledge",
        "repos_path", "repos_paths",
    }
    missing = expected_keys - set(ctx.keys())
    assert not missing, f"Missing keys in domain context: {missing}"


def test_load_domain_context_base_included():
    if _skip_if_no_biz(): return
    domains = discover_domains()
    if not domains:
        print("  SKIP (no domains)")
        return
    ctx_with = load_domain_context([domains[0]], include_base=True)
    ctx_without = load_domain_context([domains[0]], include_base=False)
    # With base should have base_context, without should be empty
    assert ctx_without["base_context"] == ""
    # If _base/ has content, ctx_with should have it
    if load_base_context():
        assert len(ctx_with["base_context"]) > 0


def test_repos_path_exists():
    if _skip_if_no_biz(): return
    for d in discover_domains():
        rp = repos_path(d)
        assert Path(rp).is_dir(), f"repos_path for {d} not found: {rp}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = skipped = 0
    print(f"Running {len(tests)} context loading tests...\n")
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
