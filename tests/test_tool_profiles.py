"""Test tool profile composition and agent tool assignments.

Verifies that profiles contain the correct tool groups and that
each agent gets the expected profile.

Usage:
    python tests/test_tool_profiles.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_profile_readonly_has_base_and_feishu_read():
    from tools import PROFILE_READONLY
    from tools.base import TOOLS as BASE_TOOLS
    from tools.feishu import FEISHU_READ, LARK_READ
    for t in BASE_TOOLS:
        assert t in PROFILE_READONLY, f"PROFILE_READONLY missing base tool: {t}"
    for t in FEISHU_READ:
        assert t in PROFILE_READONLY, f"PROFILE_READONLY missing feishu read: {t}"
    for t in LARK_READ:
        assert t in PROFILE_READONLY, f"PROFILE_READONLY missing lark read: {t}"


def test_profile_readonly_no_write():
    from tools import PROFILE_READONLY
    from tools.feishu import FEISHU_WRITE
    assert "Write" not in PROFILE_READONLY
    assert "Edit" not in PROFILE_READONLY
    assert "Bash" not in PROFILE_READONLY
    for t in FEISHU_WRITE:
        assert t not in PROFILE_READONLY, f"PROFILE_READONLY should not have write tool: {t}"


def test_profile_standard_has_feishu_write():
    from tools import PROFILE_STANDARD
    from tools.feishu import FEISHU_WRITE
    for t in FEISHU_WRITE:
        assert t in PROFILE_STANDARD, f"PROFILE_STANDARD missing feishu write: {t}"


def test_profile_standard_no_bash():
    from tools import PROFILE_STANDARD
    assert "Write" not in PROFILE_STANDARD
    assert "Edit" not in PROFILE_STANDARD
    assert "Bash" not in PROFILE_STANDARD


def test_profile_readwrite_has_everything():
    from tools import PROFILE_READWRITE
    from tools.base import TOOLS as BASE_TOOLS
    from tools.dev import TOOLS as DEV_TOOLS
    from tools.feishu import FEISHU_READ, FEISHU_WRITE, LARK_READ
    for t in BASE_TOOLS + DEV_TOOLS:
        assert t in PROFILE_READWRITE, f"PROFILE_READWRITE missing: {t}"
    for t in FEISHU_READ + FEISHU_WRITE + LARK_READ:
        assert t in PROFILE_READWRITE, f"PROFILE_READWRITE missing: {t}"


def test_profile_orchestrator_no_local_file_tools():
    from tools import PROFILE_ORCHESTRATOR
    assert "Read" not in PROFILE_ORCHESTRATOR
    assert "Glob" not in PROFILE_ORCHESTRATOR
    assert "Grep" not in PROFILE_ORCHESTRATOR
    assert "Write" not in PROFILE_ORCHESTRATOR
    assert "Edit" not in PROFILE_ORCHESTRATOR
    assert "Bash" not in PROFILE_ORCHESTRATOR


def test_profile_orchestrator_has_web_and_feishu():
    from tools import PROFILE_ORCHESTRATOR
    from tools.feishu import FEISHU_READ, FEISHU_WRITE
    assert "WebSearch" in PROFILE_ORCHESTRATOR
    assert "WebFetch" in PROFILE_ORCHESTRATOR
    for t in FEISHU_READ:
        assert t in PROFILE_ORCHESTRATOR, f"PROFILE_ORCHESTRATOR missing feishu read: {t}"
    for t in FEISHU_WRITE:
        assert t in PROFILE_ORCHESTRATOR, f"PROFILE_ORCHESTRATOR missing feishu write: {t}"


def test_dev_agent_uses_readwrite():
    import os; os.environ.setdefault("FEISHU_APP_ID", "t"); os.environ.setdefault("FEISHU_APP_SECRET", "t")
    import agents.dev
    from agents import get_agent
    from tools import PROFILE_READWRITE
    cfg = get_agent("dev")
    assert cfg is not None, "dev agent not registered"
    assert cfg.tools == PROFILE_READWRITE, f"dev tools mismatch: got {len(cfg.tools)} tools"


def test_ask_agent_uses_readonly():
    import os; os.environ.setdefault("FEISHU_APP_ID", "t"); os.environ.setdefault("FEISHU_APP_SECRET", "t")
    import agents.ask
    from agents import get_agent
    from tools import PROFILE_READONLY
    cfg = get_agent("ask")
    assert cfg is not None, "ask agent not registered"
    assert cfg.tools == PROFILE_READONLY, f"ask tools mismatch: got {len(cfg.tools)} tools"


def test_role_agent_uses_orchestrator():
    import os; os.environ.setdefault("FEISHU_APP_ID", "t"); os.environ.setdefault("FEISHU_APP_SECRET", "t")
    import agents.role
    from agents import get_agent
    from tools import PROFILE_ORCHESTRATOR
    cfg = get_agent("role")
    assert cfg is not None, "role agent not registered"
    assert cfg.tools == PROFILE_ORCHESTRATOR, f"role tools mismatch: got {len(cfg.tools)} tools"


def test_no_duplicate_tools_in_profiles():
    from tools import PROFILE_READONLY, PROFILE_STANDARD, PROFILE_READWRITE, PROFILE_ORCHESTRATOR
    for name, profile in [
        ("READONLY", PROFILE_READONLY),
        ("STANDARD", PROFILE_STANDARD),
        ("READWRITE", PROFILE_READWRITE),
        ("ORCHESTRATOR", PROFILE_ORCHESTRATOR),
    ]:
        assert len(profile) == len(set(profile)), f"{name} has duplicate tools"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    print(f"Running {len(tests)} tool profile tests...\n")
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
