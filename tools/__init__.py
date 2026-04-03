"""Tool registry and profile composition.

Architecture (aligned with Claude Code's tool system):

    SDK Built-in Tools (Claude Agent SDK provides these automatically)
    ├── File: Read, Write, Edit, Glob, Grep
    ├── Execution: Bash
    ├── Web: WebSearch, WebFetch
    ├── Workflow: EnterPlanMode, ExitPlanMode, TodoWrite
    └── Agent: Task (sub-agent delegation)

    MCP Tools (external servers, configured in mcp.json)
    ├── Feishu: fetch-doc, create-doc, update-doc, search-doc, ...
    ├── Lark: bitable, chat, message, wiki, ...
    ├── GitLab: projects, files, merge_requests, ...
    └── Ops: query_prometheus, sls_execute_sql, ...

    Profiles (tool combinations for different agent roles)
    ├── READONLY:     base + feishu_read + lark_read + gitlab
    ├── STANDARD:     base + feishu_rw + lark_read + gitlab
    ├── READWRITE:    base + dev + feishu_rw + lark_read + browser + gitlab
    └── ORCHESTRATOR: web + feishu_rw + lark_read + gitlab

    Safety Hooks (PreToolUse guards)
    ├── BashGuard: blocks exploration cmds → redirect to Read/Glob/Grep
    ├── GitGuard: blocks destructive git ops → redirect to /dev push
    └── DestructiveGuard: blocks rm -rf, mkfs, dd, fork bomb, curl|bash
"""

import fnmatch


def compose_tools(*groups: list[str]) -> list[str]:
    """Merge multiple tool groups, preserving order, deduplicating."""
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for tool in group:
            if tool not in seen:
                seen.add(tool)
                result.append(tool)
    return result


def filter_by_deny(tools: list[str], deny_patterns: list[str]) -> list[str]:
    """Remove tools matching any deny pattern (supports wildcards).

    Example:
        filter_by_deny(all_tools, ["mcp__agent-gitlab-mcp__*"])
        → removes all gitlab MCP tools
    """
    if not deny_patterns:
        return tools
    return [
        t for t in tools
        if not any(fnmatch.fnmatch(t, p) for p in deny_patterns)
    ]


# ── Import tool groups ──

from tools.base import TOOLS as BASE_TOOLS, READONLY_TOOLS, WEB_TOOLS
from tools.dev import TOOLS as DEV_TOOLS
from tools.feishu import FEISHU_READ, FEISHU_WRITE, LARK_READ
from tools.browser import BROWSER_TOOLS
from tools.gitlab import GITLAB_ALL

# ── Standard profiles ──

PROFILE_READONLY = compose_tools(BASE_TOOLS, FEISHU_READ, LARK_READ, GITLAB_ALL)
PROFILE_STANDARD = compose_tools(BASE_TOOLS, FEISHU_READ, FEISHU_WRITE, LARK_READ, GITLAB_ALL)
PROFILE_READWRITE = compose_tools(BASE_TOOLS, DEV_TOOLS, FEISHU_READ, FEISHU_WRITE, LARK_READ, BROWSER_TOOLS, GITLAB_ALL)
PROFILE_ORCHESTRATOR = compose_tools(WEB_TOOLS, FEISHU_READ, FEISHU_WRITE, LARK_READ, GITLAB_ALL)
