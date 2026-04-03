"""MCP server management: config loading, auth injection, tool filtering, routing.

Inspired by Claude Code's MCP client architecture (src/services/mcp/client.ts):
- Centralized config loading with credential injection
- TAT token caching with TTL-based refresh
- Permission-based server filtering (group → allowed servers)
- Query-based server routing (user message → relevant servers)
- Health status tracking

Public API:
    load(path)                              — load mcp.json at startup
    init(servers)                           — inject directly (for tests)
    get_servers(group, allowed_tools)       — filtered + auth-injected servers
    get_servers_for_query(group, allowed_tools, query) — + query-based routing
    get_server_names()                      — list loaded server names
    get_health()                            — server health summary
"""

import copy
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Module state ──

_mcp_servers: dict | None = None
_tat_cache: tuple[str, float] | None = None  # (token, expiry_time)
_TAT_TTL = 7000  # seconds (~2h, TAT expires in 2h but we refresh early)

# ── Server health tracking ──

_server_errors: dict[str, int] = {}  # server_name → consecutive error count
_MAX_ERRORS_BEFORE_WARN = 3


# ══════════════════════════════════════════════════════════════════════════════
# Config Loading
# ══════════════════════════════════════════════════════════════════════════════


def load(path: str = "") -> dict:
    """Load MCP server configs from mcp.json. Stores result in module state.

    Credential placeholders (YOUR_APP_ID, etc.) are replaced with values
    from .env so mcp.json can stay as a template without hardcoded secrets.
    """
    global _mcp_servers
    if not path:
        from core.config import get_settings
        path = get_settings().mcp_config_path
    config_path = Path(path)
    if not config_path.exists():
        logger.warning("MCP config not found at %s", config_path)
        _mcp_servers = {}
        return _mcp_servers
    try:
        raw = config_path.read_text()
        raw = _inject_credentials(raw)
        data = json.loads(raw)
        _mcp_servers = data.get("mcpServers", {})
        logger.info("Loaded MCP servers: %s", list(_mcp_servers.keys()))
        return _mcp_servers
    except Exception:
        logger.exception("Failed to load MCP config from %s", config_path)
        _mcp_servers = {}
        return _mcp_servers


def _inject_credentials(raw: str) -> str:
    """Replace credential placeholders in mcp.json with actual values from .env."""
    from core.config import get_settings
    s = get_settings()
    replacements = {
        "YOUR_APP_ID": s.feishu_app_id,
        "YOUR_APP_SECRET": s.feishu_app_secret,
    }
    if s.feishu_personal_token:
        replacements["YOUR_PERSONAL_TOKEN"] = s.feishu_personal_token
    if s.feishu_project_token:
        replacements["YOUR_PROJECT_TOKEN"] = s.feishu_project_token
    for placeholder, value in replacements.items():
        raw = raw.replace(placeholder, value)
    return raw


def init(servers: dict) -> None:
    """Inject MCP server config directly (for testing)."""
    global _mcp_servers
    _mcp_servers = servers


def get_server_names() -> list[str]:
    """Return list of loaded MCP server names."""
    return list((_mcp_servers or {}).keys())


# ══════════════════════════════════════════════════════════════════════════════
# TAT Token Management (cached with TTL)
# ══════════════════════════════════════════════════════════════════════════════


def _get_tenant_access_token() -> str:
    """Get current valid tenant_access_token, with caching.

    lark-oapi internally caches tokens, but we add an extra layer to avoid
    repeated cross-module calls. Cache TTL is ~2h (TAT expires in 2h).
    """
    global _tat_cache
    now = time.time()
    if _tat_cache and now < _tat_cache[1]:
        return _tat_cache[0]

    from core.card import _get_client
    from lark_oapi.core.token.manager import TokenManager
    client = _get_client()
    token = TokenManager.get_self_tenant_token(client._config)
    _tat_cache = (token, now + _TAT_TTL)
    return token


def _inject_feishu_tat(servers: dict) -> dict:
    """Inject current TAT into feishu-mcp HTTP server headers."""
    key = "agent-feishu-mcp"
    if key not in servers:
        return servers
    srv = servers[key]
    if srv.get("type") != "http":
        return servers
    try:
        tat = _get_tenant_access_token()
        headers = dict(srv.get("headers") or {})
        headers["X-Lark-MCP-TAT"] = tat
        srv["headers"] = headers
        servers[key] = srv
    except Exception:
        logger.warning("Failed to inject TAT for feishu-mcp, skipping")
    return servers


# ══════════════════════════════════════════════════════════════════════════════
# Feishu Tool Filtering (header-based permission control)
# ══════════════════════════════════════════════════════════════════════════════

_FEISHU_MCP_READ_NAMES = "fetch-doc,list-docs,get-comments,get-user,fetch-file"
_FEISHU_MCP_WRITE_NAMES = "create-doc,update-doc,add-comments"
_FEISHU_WRITE_TOOLS = [
    f"mcp__agent-feishu-mcp__{name}" for name in _FEISHU_MCP_WRITE_NAMES.split(",")
]


def _filter_feishu_mcp_tools(servers: dict, allowed_tools: list[str] | None) -> None:
    """Set X-Lark-MCP-Allowed-Tools header based on effective allowed_tools.

    The SDK's allowed_tools does not restrict MCP server tools — the server
    exposes whatever the header permits. We must align the header with the
    user's actual permissions.
    """
    key = "agent-feishu-mcp"
    if key not in servers or not allowed_tools:
        return

    has_write = (
        any(t == "mcp__agent-feishu-mcp__*" for t in allowed_tools)
        or any(t in allowed_tools for t in _FEISHU_WRITE_TOOLS)
    )

    tool_names = (
        f"{_FEISHU_MCP_READ_NAMES},{_FEISHU_MCP_WRITE_NAMES}"
        if has_write
        else _FEISHU_MCP_READ_NAMES
    )

    headers = dict(servers[key].get("headers") or {})
    headers["X-Lark-MCP-Allowed-Tools"] = tool_names
    servers[key]["headers"] = headers
    logger.info("feishu-mcp tools: %s (write=%s)", tool_names, has_write)


# ══════════════════════════════════════════════════════════════════════════════
# Query-Based Server Routing (reduce token overhead)
# ══════════════════════════════════════════════════════════════════════════════

# Per-server keywords: if any keyword appears in user message, server is included.
# Servers not listed here are always included (no filtering).
_SERVER_TAGS: dict[str, set[str]] = {
    "agent-feishu-mcp": {
        "文档", "飞书", "feishu", "lark", "doc", "wiki", "知识库",
        "创建文档", "编辑文档", "搜索文档", "评论",
    },
    "agent-feishu-mcp-uat": {
        "文档", "飞书", "feishu", "lark", "doc", "wiki", "知识库",
        "搜索文档", "搜索用户", "search",
    },
    "agent-lark-mcp": {
        "多维表格", "bitable", "表格", "群聊", "群消息", "消息",
        "chat", "message", "群", "联系人", "wiki",
    },
    "agent-gitlab-mcp": {
        "代码", "code", "gitlab", "git", "仓库", "repo", "mr", "merge",
        "分支", "branch", "commit", "项目代码", "源码", "pull",
        "pipeline", "ci", "issue",
    },
    "ops-mcp": {
        "监控", "prometheus", "指标", "metric", "cpu", "内存", "memory",
        "qps", "延迟", "latency", "告警", "alert",
    },
    "observability-mcp": {
        "日志", "log", "sls", "trace", "排查", "故障", "错误",
        "异常", "exception", "error",
    },
}


def _route_by_query(servers: dict, query: str) -> dict:
    """Filter MCP servers by keyword relevance to user query.

    Safe fallback: if no keywords match, returns all servers.
    """
    if not servers or not query:
        return servers

    msg_lower = query.lower()
    matched: dict = {}
    unmatched: list[str] = []

    for name, config in servers.items():
        tags = _SERVER_TAGS.get(name)
        if tags is None:
            matched[name] = config  # no tags = always include
        elif any(tag in msg_lower for tag in tags):
            matched[name] = config
        else:
            unmatched.append(name)

    if not matched:
        return servers  # safe fallback

    if unmatched:
        logger.info(
            "[MCP Router] Filtered: kept=%s, removed=%s",
            list(matched.keys()), unmatched,
        )

    return matched


# ══════════════════════════════════════════════════════════════════════════════
# Health Tracking
# ══════════════════════════════════════════════════════════════════════════════


def record_error(server_name: str) -> None:
    """Record a consecutive error for a server. Logs warning after threshold."""
    count = _server_errors.get(server_name, 0) + 1
    _server_errors[server_name] = count
    if count == _MAX_ERRORS_BEFORE_WARN:
        logger.warning("[MCP Health] Server '%s' has %d consecutive errors", server_name, count)


def record_success(server_name: str) -> None:
    """Reset error count on successful call."""
    _server_errors.pop(server_name, None)


def get_health() -> dict[str, str]:
    """Return health status for each server: 'ok' or 'degraded (N errors)'."""
    result = {}
    for name in (_mcp_servers or {}):
        errors = _server_errors.get(name, 0)
        result[name] = "ok" if errors < _MAX_ERRORS_BEFORE_WARN else f"degraded ({errors} errors)"
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def get_servers(group: str = "admin", allowed_tools: list[str] | None = None) -> dict:
    """Get MCP servers with auth injection and permission filtering.

    - Non-admin groups: UAT server removed
    - feishu-mcp: X-Lark-MCP-Allowed-Tools header set per permissions
    - feishu-mcp: TAT token injected (cached)
    """
    servers = copy.deepcopy(_mcp_servers or {})
    if group != "admin":
        if servers.pop("agent-feishu-mcp-uat", None):
            logger.info("MCP: removed UAT server for group=%s", group)
    _filter_feishu_mcp_tools(servers, allowed_tools)
    logger.info("MCP servers for group=%s: %s", group, list(servers.keys()))
    return _inject_feishu_tat(servers)


def get_servers_for_query(
    group: str = "admin",
    allowed_tools: list[str] | None = None,
    query: str = "",
) -> dict:
    """Get MCP servers filtered by both permissions AND query relevance.

    Use this for orchestrator sub-agents to reduce token overhead.
    Falls back to get_servers() if no query provided.
    """
    servers = get_servers(group, allowed_tools)
    if query:
        servers = _route_by_query(servers, query)
    return servers
