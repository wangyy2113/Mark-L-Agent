"""End-to-end integration test: Permissions → MCP filtering pipeline.

Simulates the full request chain: Permissions resolves group → core.mcp
filters servers/tools → verify output. Covers UAT access control for all
permission groups including admin sudo.

Does NOT require API keys, running services, or claude_agent_sdk.

Usage:
    python tests/test_uat_e2e.py
"""

import copy
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import core.mcp
from core.permissions import Permissions, GroupConfig

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
FEISHU_UAT = ["mcp__agent-feishu-mcp-uat__*"]

# ── Test fixtures ──

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

ADMIN_ID = "ou_admin_001"
DEV_ID = "ou_dev_001"
MEMBER_ID = "ou_member_001"
STRANGER_ID = "ou_stranger_999"
CHAT_A = "oc_chat_a"

# Tool groups matching DEFAULT_GROUPS in permissions.py
ADMIN_TOOLS = ["all"]
DEVELOPER_TOOLS = ["base", "dev", "feishu_read", "feishu_write", "lark_read"]
MEMBER_TOOLS = ["base", "feishu_read", "lark_read"]

# Simple tool resolution (mirrors agent.resolve_group_tools without SDK import)
TOOL_ALIASES = {
    "all": list(FEISHU_READ) + list(FEISHU_WRITE) + FEISHU_UAT + ["Read", "Write", "Bash"],
    "base": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
    "dev": ["Write", "Edit", "Bash"],
    "feishu_read": list(FEISHU_READ),
    "feishu_write": list(FEISHU_WRITE),
    "lark_read": ["mcp__agent-lark-mcp__*"],
}


def _resolve_tools(tool_names: list[str]) -> list[str]:
    """Simplified tool resolution for testing."""
    result = []
    seen = set()
    for name in tool_names:
        expanded = TOOL_ALIASES.get(name, [name])
        for tool in expanded:
            if tool not in seen:
                seen.add(tool)
                result.append(tool)
    return result


def _make_perms(data: dict) -> Permissions:
    """Create a Permissions instance backed by a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.write(json.dumps(data).encode())
    tmp.close()
    return Permissions(path=tmp.name)


def _setup():
    """Set up permissions + MCP for each test."""
    core.mcp.init(copy.deepcopy(MOCK_MCP))
    perms = _make_perms({
        "admins": [ADMIN_ID],
        "groups": {"developer": [DEV_ID], "member": [MEMBER_ID]},
        "chats": {},
    })
    return perms


def _get_servers_for_user(perms: Permissions, sender_id: str, chat_id: str = CHAT_A):
    """Simulate the request pipeline: resolve group → resolve tools → get MCP servers.

    Returns (group, servers) or (None, None) if denied.
    """
    group = perms.get_group(sender_id, chat_id)
    if group is None:
        return None, None

    group_cfg = perms.get_group_config(group)
    if group_cfg and group_cfg.tools:
        allowed_tools = _resolve_tools(group_cfg.tools)
    else:
        allowed_tools = None  # admin default: no filter

    servers = core.mcp.get_servers(group, allowed_tools)
    return group, servers


# ── Tests ──

def test_admin_gets_uat_and_all_servers():
    """Admin gets UAT server + all other servers, with full read+write tools."""
    perms = _setup()
    group, servers = _get_servers_for_user(perms, ADMIN_ID)

    assert group == "admin"
    assert "agent-feishu-mcp-uat" in servers, "Admin must have UAT server"
    assert "agent-feishu-mcp" in servers
    assert "agent-lark-mcp" in servers
    assert len(servers) == 3

    # Admin with "all" tools gets both read and write in the header
    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "fetch-doc" in header, f"Admin should have read tools, got: {header}"
    assert "create-doc" in header, f"Admin should have write tools, got: {header}"


def test_developer_no_uat_has_write():
    """Developer group: no UAT server, feishu header includes write tools."""
    perms = _setup()
    group, servers = _get_servers_for_user(perms, DEV_ID)

    assert group == "developer"
    assert "agent-feishu-mcp-uat" not in servers, "Developer must NOT have UAT server"
    assert "agent-feishu-mcp" in servers
    assert "agent-lark-mcp" in servers

    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" in header, f"Developer should have write tools, got: {header}"
    assert "fetch-doc" in header, f"Developer should have read tools, got: {header}"


def test_member_no_uat_readonly():
    """Member group: no UAT server, feishu header is read-only."""
    perms = _setup()
    group, servers = _get_servers_for_user(perms, MEMBER_ID)

    assert group == "member"
    assert "agent-feishu-mcp-uat" not in servers, "Member must NOT have UAT server"
    assert "agent-feishu-mcp" in servers

    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" not in header, f"Member should NOT have write tools, got: {header}"
    assert "fetch-doc" in header, f"Member should have read tools, got: {header}"


def test_stranger_denied():
    """Unknown user is denied at permission gate — never reaches MCP."""
    perms = _setup()
    group, servers = _get_servers_for_user(perms, STRANGER_ID)

    assert group is None, "Stranger should have no group"
    assert servers is None, "Stranger should get no servers"


def test_admin_sudo_as_member_removes_uat():
    """Admin using sudo to impersonate member: UAT stripped, read-only tools."""
    perms = _setup()
    perms.set_sudo(CHAT_A, "member")

    group, servers = _get_servers_for_user(perms, ADMIN_ID, CHAT_A)

    assert group == "member", "Sudo should downgrade admin to member"
    assert "agent-feishu-mcp-uat" not in servers, "Sudo member must NOT have UAT"

    header = servers["agent-feishu-mcp"]["headers"].get("X-Lark-MCP-Allowed-Tools", "")
    assert "create-doc" not in header, f"Sudo member should be read-only, got: {header}"
    assert "fetch-doc" in header

    # Clear sudo and verify admin is restored
    perms.clear_sudo(CHAT_A)
    group2, servers2 = _get_servers_for_user(perms, ADMIN_ID, CHAT_A)
    assert group2 == "admin"
    assert "agent-feishu-mcp-uat" in servers2, "Admin restored after sudo clear"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    print(f"Running {len(tests)} UAT e2e tests...\n")
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
