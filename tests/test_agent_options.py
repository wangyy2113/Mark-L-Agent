"""Test agent options building logic.

Verifies that _build_options, _build_agent_options, and _build_orchestrator_options
produce correct configurations for different groups and agents.

Usage:
    python tests/test_agent_options.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
os.environ.setdefault("FEISHU_APP_ID", "t")
os.environ.setdefault("FEISHU_APP_SECRET", "t")

# Load agents
from main import _load_agents
_, _raw_config = _load_agents()
from agents import apply_config_overrides
apply_config_overrides(_raw_config)

import agent
from core.session import SessionStore
from core.agent_session import AgentSessionStore, AgentState
from core.permissions import Permissions


def _init_agent():
    """Initialize agent module with mock stores."""
    perms = Permissions()
    agent.init(SessionStore(), AgentSessionStore(), perms)


_init_agent()


# ── Chat mode options ──

def test_chat_admin_has_all_tools():
    opts = agent._build_options("test_chat", group="admin")
    assert "Bash" in opts.allowed_tools
    assert "Read" in opts.allowed_tools
    assert any("feishu-mcp" in t for t in opts.allowed_tools)


def test_chat_has_mcp_servers():
    opts = agent._build_options("test_chat", group="admin")
    assert opts.mcp_servers is not None
    assert len(opts.mcp_servers) > 0


# ── Agent mode options ──

def test_dev_agent_has_feishu_write():
    from agents import get_agent
    cfg = get_agent("dev")
    state = AgentState(active=True, agent_name="dev", domains=["ca"])
    opts = agent._build_agent_options("test_chat", cfg, state, group="admin")
    has_create_doc = any("create-doc" in t for t in opts.allowed_tools)
    assert has_create_doc, "dev agent should have feishu create-doc"


def test_dev_agent_has_bash():
    from agents import get_agent
    cfg = get_agent("dev")
    state = AgentState(active=True, agent_name="dev", domains=["ca"])
    opts = agent._build_agent_options("test_chat", cfg, state, group="admin")
    assert "Bash" in opts.allowed_tools


def test_ask_agent_no_write():
    from agents import get_agent
    cfg = get_agent("ask")
    state = AgentState(active=True, agent_name="ask", domains=["ca"])
    opts = agent._build_agent_options("test_chat", cfg, state, group="admin")
    assert "Bash" not in opts.allowed_tools
    assert "Write" not in opts.allowed_tools
    has_create_doc = any("create-doc" in t for t in opts.allowed_tools)
    assert not has_create_doc, "ask agent should NOT have feishu create-doc"


def test_dev_agent_non_admin_removes_uat():
    from agents import get_agent
    cfg = get_agent("dev")
    state = AgentState(active=True, agent_name="dev", domains=["ca"])
    opts = agent._build_agent_options("test_chat", cfg, state, group="developer")
    if opts.mcp_servers:
        assert "agent-feishu-mcp-uat" not in opts.mcp_servers, "UAT should be removed for developer"


def test_dev_agent_feishu_header_has_write():
    """dev agent's feishu-mcp header should expose write tools."""
    from agents import get_agent
    cfg = get_agent("dev")
    state = AgentState(active=True, agent_name="dev", domains=["ca"])
    opts = agent._build_agent_options("test_chat", cfg, state, group="developer")
    if opts.mcp_servers and "agent-feishu-mcp" in opts.mcp_servers:
        header = opts.mcp_servers["agent-feishu-mcp"].get("headers", {}).get("X-Lark-MCP-Allowed-Tools", "")
        assert "create-doc" in header, f"dev agent feishu header should include write tools, got: {header}"


# ── Orchestrator options ──

def test_orchestrator_only_task():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    assert opts.allowed_tools == ["Task"], f"Orchestrator should only have Task, got: {opts.allowed_tools}"


def test_orchestrator_no_disallowed():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    assert not opts.disallowed_tools, f"Orchestrator should have no disallowed_tools, got: {opts.disallowed_tools}"


def test_orchestrator_no_mcp():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    assert not opts.mcp_servers, f"Orchestrator should have no MCP servers, got: {list(opts.mcp_servers.keys()) if opts.mcp_servers else []}"


def test_orchestrator_has_agents():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    assert opts.agents is not None
    assert len(opts.agents) >= 2, f"Orchestrator should have at least 2 sub-agents, got: {list(opts.agents.keys()) if opts.agents else []}"


def test_orchestrator_sub_agents_have_tools():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    for name, defn in opts.agents.items():
        assert defn.tools, f"Sub-agent {name} has no tools"
        assert "Read" in defn.tools, f"Sub-agent {name} missing Read"
        assert "Glob" in defn.tools, f"Sub-agent {name} missing Glob"


def test_orchestrator_sub_agents_have_mcp():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    for name, defn in opts.agents.items():
        assert defn.mcpServers is not None, f"Sub-agent {name} has no MCP servers"
        assert len(defn.mcpServers) > 0, f"Sub-agent {name} has empty MCP servers"


def test_orchestrator_dev_sub_agent_has_write():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    dev = opts.agents.get("dev")
    assert dev is not None, "No dev sub-agent"
    assert "Write" in dev.tools, "dev sub-agent missing Write"
    assert "Bash" in dev.tools, "dev sub-agent missing Bash"


def test_orchestrator_ask_sub_agent_no_write():
    opts = agent._build_orchestrator_options("test_chat", "test_sender", "admin")
    ask = opts.agents.get("ask")
    assert ask is not None, "No ask sub-agent"
    assert "Write" not in ask.tools, "ask sub-agent should not have Write"
    assert "Bash" not in ask.tools, "ask sub-agent should not have Bash"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    print(f"Running {len(tests)} agent options tests...\n")
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
