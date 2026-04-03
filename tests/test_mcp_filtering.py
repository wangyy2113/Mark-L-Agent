"""Test MCP server filtering logic.

Verifies UAT removal for non-admin groups and feishu-mcp header filtering.
Does NOT require API keys, running services, or claude_agent_sdk.

Usage:
    python tests/test_mcp_filtering.py
"""

import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import core.mcp

# Inline feishu tool lists (avoids importing tools.feishu which pulls in claude_agent_sdk)
FEISHU_READ = [
    "mcp__agent-feishu-mcp__fetch-doc",
    "mcp__agent-feishu-mcp__list-docs",
    "mcp__agent-feishu-mcp__get-comments",
    "mcp__agent-feishu-mcp__get-user",
    "mcp__agent-feishu-mcp__fetch-file",
]
FEISHU_WRITE = [
    "mcp__agent-feishu-mcp__create-doc",
    "mcp__agent-feishu-mcp__update-doc",
    "mcp__agent-feishu-mcp__add-comments",
]

# Mock MCP config (simulates what mcp.json contains)
MOCK_MCP = {
    "agent-feishu-mcp": {
        "type": "http",
        "url": "https://mcp.feishu.cn/mcp",
        "headers": {},
    },
    "agent-feishu-mcp-uat": {
        "type": "http",
        "url": "https://mcp.feishu.cn/mcp/personal",
    },
    "agent-lark-mcp": {
        "command": "npx",
        "args": ["-y", "@larksuiteoapi/lark-mcp"],
    },
}


def _setup_mock_mcp():
    """Inject mock MCP config into core.mcp module."""
    core.mcp.init(copy.deepcopy(MOCK_MCP))


def test_admin_keeps_all_servers():
    _setup_mock_mcp()
    servers = core.mcp.get_servers(group="admin", allowed_tools=None)
    assert "agent-feishu-mcp" in servers
    assert "agent-feishu-mcp-uat" in servers
    assert "agent-lark-mcp" in servers
    assert len(servers) == 3


def test_non_admin_removes_uat():
    _setup_mock_mcp()
    servers = core.mcp.get_servers(group="developer", allowed_tools=["Read"])
    assert "agent-feishu-mcp" in servers
    assert "agent-feishu-mcp-uat" not in servers, "UAT should be removed for non-admin"
    assert "agent-lark-mcp" in servers
    assert len(servers) == 2


def test_member_removes_uat():
    _setup_mock_mcp()
    servers = core.mcp.get_servers(group="member", allowed_tools=["Read"])
    assert "agent-feishu-mcp-uat" not in servers


def test_feishu_header_readonly_when_no_write_tools():
    _setup_mock_mcp()
    servers = core.mcp.get_servers(group="admin", allowed_tools=list(FEISHU_READ))
    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" not in header, f"Should not expose write tools, got: {header}"
    assert "fetch-doc" in header, f"Should expose read tools, got: {header}"


def test_feishu_header_includes_write_when_write_tools():
    _setup_mock_mcp()
    servers = core.mcp.get_servers(group="admin", allowed_tools=list(FEISHU_READ) + list(FEISHU_WRITE))
    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" in header, f"Should expose write tools, got: {header}"
    assert "fetch-doc" in header, f"Should expose read tools, got: {header}"


def test_feishu_header_no_filter_for_admin_wildcard():
    _setup_mock_mcp()
    # Admin with no allowed_tools = full access, no header override
    servers = core.mcp.get_servers(group="admin", allowed_tools=None)
    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert header == "", "Admin with no filter should not set allowed-tools header"


def test_feishu_header_write_via_wildcard():
    _setup_mock_mcp()
    # Wildcard should be treated as having write access
    servers = core.mcp.get_servers(group="admin", allowed_tools=["mcp__agent-feishu-mcp__*"])
    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" in header, f"Wildcard should grant write, got: {header}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    print(f"Running {len(tests)} MCP filtering tests...\n")
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
