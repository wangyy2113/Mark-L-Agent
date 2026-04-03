"""Claude Agent SDK integration — the core of the bot."""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, tool as sdk_tool, create_sdk_mcp_server

import core.audit as audit
from core.config import get_settings
from core.agent_session import AgentSessionStore
from core.permissions import Permissions
from core.session import SessionStore

logger = logging.getLogger(__name__)

# ── Globals ──
_session_store: SessionStore | None = None
_agent_session_store: AgentSessionStore | None = None
_permissions: Permissions | None = None
_usage_store = None  # type: core.usage.UsageStore | None

# Per-chat concurrency locks
_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_meta = threading.Lock()

# Per-chat cancel signals
_cancel_events: dict[str, threading.Event] = {}
_cancel_meta = threading.Lock()

# ── Model aliases + per-chat override ──

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Per-chat model override (in-memory, cleared on restart)
_chat_models: dict[str, str] = {}


def resolve_model(alias: str) -> str | None:
    """Resolve a model alias to full model ID. Returns None if invalid."""
    alias = alias.strip().lower()
    if alias in MODEL_ALIASES:
        return MODEL_ALIASES[alias]
    # Accept full model IDs that match a known alias value
    if alias in MODEL_ALIASES.values():
        return alias
    return None


def set_chat_model(chat_id: str, alias_or_id: str) -> str | None:
    """Set per-chat model override. Returns resolved model ID, or None if invalid."""
    model_id = resolve_model(alias_or_id)
    if model_id:
        _chat_models[chat_id] = model_id
    return model_id


def clear_chat_model(chat_id: str) -> None:
    """Clear per-chat model override."""
    _chat_models.pop(chat_id, None)


def get_effective_model(chat_id: str, agent_cfg=None) -> str:
    """Resolve model by priority: per-chat override > per-agent default > global."""
    # 1. Per-chat override (set via /model command)
    chat_model = _chat_models.get(chat_id)
    if chat_model:
        return chat_model
    # 2. Per-agent default (AgentConfig.model)
    if agent_cfg and getattr(agent_cfg, "model", ""):
        resolved = resolve_model(agent_cfg.model)
        if resolved:
            return resolved
    # 3. Global default
    return get_settings().claude_model

# ── Shared help data (single source of truth) ──
# Used by both SYSTEM_PROMPT and event_handler card builders.

CAPABILITIES = [
    "回答问题、分析代码、写文档",
    "执行 Shell 命令、读写文件",
    "查看用户发送的图片和文件（PDF 等）",
    "搜索/创建/编辑飞书文档",
    "查询/操作多维表格",
    "搜索网页内容",
]

COMMANDS = [
    ("/help", "查看完整帮助"),
    ("/stop", "停止当前请求"),
    ("/clear", "清除对话历史"),
    ("/session", "查看当前会话状态"),
    ("/model", "查看/切换模型"),
    ("/agent list", "列出已注册 Agent"),
    ("/agent done", "退出当前 Agent 模式"),
    ("/admin help", "管理员命令帮助"),
]

_identity_md_cache: str | None = None


def _load_identity_md() -> str:
    """Load identity.md from project root (cached after first read)."""
    global _identity_md_cache
    if _identity_md_cache is not None:
        return _identity_md_cache
    identity_path = Path(__file__).parent / "identity.md"
    if identity_path.is_file():
        _identity_md_cache = identity_path.read_text(encoding="utf-8").strip()
        logger.info("Loaded identity.md (%d chars)", len(_identity_md_cache))
    else:
        _identity_md_cache = ""
    return _identity_md_cache


def build_system_prompt() -> str:
    """Build the chat system prompt dynamically from shared data + agent registry."""
    from agents import list_agents
    from core.config import get_settings

    settings = get_settings()
    bot_name = settings.bot_name or "Mark-L Agent"
    bot_tagline = settings.bot_tagline or ""

    cap_lines = "\n".join(f"{i+1}. {c}" for i, c in enumerate(CAPABILITIES))
    cmd_lines = "\n".join(f"- {cmd} — {desc}" for cmd, desc in COMMANDS)

    agent_cmd_lines = ""
    agents = list_agents()
    if agents:
        agent_cmd_lines = "\n".join(f"- {a.command} — {a.display_name}（{a.description}）" for a in agents)

    # Identity: bot name + tagline from .env, project info from identity.md
    tagline_part = f"（{bot_tagline}）" if bot_tagline else ""
    identity_md = _load_identity_md()
    identity_block = f"\n{identity_md}\n" if identity_md else ""

    parts = [
        f"你是 {bot_name}{tagline_part}，一个基于 Mark-L Agent 平台运行的 AI 协作助手。"
        f"{identity_block}"
        f"\n你可以：\n",
        cap_lines,
        "\n\n使用工具时：\n"
        "- 先理解用户意图，必要时先搜索再操作\n"
        "- 操作多维表格前，先了解表结构\n"
        "\n安全规则（必须遵守）：\n"
        "- 绝对禁止执行以下命令：rm -rf /、rm -rf ~、mkfs、dd if=/dev/zero、:(){ :|:& };: 等破坏性命令\n"
        "- 删除文件/目录前必须先告知用户具体路径并确认意图，不要主动执行 rm -rf\n"
        "- 不要执行任何可能导致数据丢失的不可逆操作\n"
        "- 如果用户要求执行危险命令，拒绝并解释风险\n"
        "- 返回结果时用简洁的中文总结，不要直接输出原始 JSON\n"
        "- 命令输出过长时提取关键信息总结\n"
        "\n可用命令（用户可以直接输入这些命令）：\n",
        cmd_lines,
    ]
    if agent_cmd_lines:
        parts.append("\n" + agent_cmd_lines)
    parts.append(
        "\n\n当用户询问你是谁、你的功能、能力、怎么用、能做什么等问题时，基于以上信息自然回答。"
        " 如果用户想深入了解项目架构、设计理念或扩展机制，读取 README.md 获取完整信息。"
        " 建议用户输入 /help 查看交互式帮助卡片。"
        "\n\n用中文回复。"
    )
    return "".join(parts)

# ── Output style guide (appended to all prompts) ──

OUTPUT_STYLE_GUIDE = """
回复格式指引（你的回复会渲染在飞书消息卡片中）：
- 标题用 #### 起步，不要用 #、## 或 ###（在卡片中太大）；可加 emoji 前缀增强辨识度，如 #### 🔍 分析结果
- 所有 block 元素（标题、表格、代码块、引用、分隔线）前后必须空一行
- 较长回复先给一句话结论，再展开细节
- 简短问题直接回答，不要过度格式化"""


# ── Feishu doc auto-grant hint ──

def _build_feishu_write_hint(sender_id: str, allowed_tools: list[str]) -> str:
    """Build prompt hint for auto-granting permission after creating Feishu docs.

    TAT-created docs belong to the app, not the user. The model must grant
    the sender edit permission immediately after creation.
    """
    if not sender_id:
        return ""
    has_write = (
        any(t == "mcp__agent-feishu-mcp__*" for t in allowed_tools)
        or any(t in allowed_tools for t in FEISHU_WRITE)
    )
    if not has_write:
        return ""
    # TODO: 开通 drive:permission:member:create 权限后恢复自动授权指引
    s = get_settings()
    folder_hint = ""
    if s.feishu_doc_folder_token:
        folder_hint = f"\n- 创建文档时必须指定 folder_token: {s.feishu_doc_folder_token}"
    return (
        f"\n\n飞书文档提示："
        f"\n- 当前用户 open_id: {sender_id}"
        f"\n- 通过 create-doc 创建的文档归属应用，创建后请告知用户文档链接"
        f"{folder_hint}"
    )


# ── Role-based tool sets ──
# Composed from tools/ modules. See tools/base.py, tools/dev.py, tools/feishu.py.

from tools import compose_tools
from tools.base import TOOLS as BASE_TOOLS
from tools.dev import TOOLS as DEV_TOOLS
from tools.feishu import (
    FEISHU_READ, FEISHU_WRITE, FEISHU_ALL, FEISHU_UAT,
    LARK_READ, LARK_WRITE, LARK_ALL,
)
from tools.browser import (
    PLAYWRIGHT_TOOLS, CHROME_DEVTOOLS_TOOLS, BROWSER_TOOLS,
    PLAYWRIGHT_ALL, CHROME_DEVTOOLS_ALL, BROWSER_ALL,
)

TOOLS_ADMIN = compose_tools(BASE_TOOLS, DEV_TOOLS, FEISHU_ALL, FEISHU_UAT, LARK_ALL, BROWSER_ALL)

# All individual tool names (no wildcards) — used for computing disallowed_tools.
# MCP wildcards crash the SDK CLI when in disallowed_tools, so we need this
# expanded list for safe comparison.
_ALL_INDIVIDUAL_TOOLS = compose_tools(
    BASE_TOOLS, DEV_TOOLS, FEISHU_READ, FEISHU_WRITE, LARK_READ, LARK_WRITE,
    BROWSER_TOOLS,
)

# Tool group aliases for permission_groups config resolution
TOOL_ALIASES: dict[str, list[str]] = {
    "all": TOOLS_ADMIN,
    "base": BASE_TOOLS,
    "dev": DEV_TOOLS,
    "feishu_read": FEISHU_READ,
    "feishu_write": FEISHU_WRITE,
    "feishu_all": FEISHU_ALL,
    "feishu_uat": FEISHU_UAT,
    "lark_read": LARK_READ,
    "lark_write": LARK_WRITE,
    "lark_all": LARK_ALL,
    "browser": BROWSER_TOOLS,
    "playwright": PLAYWRIGHT_TOOLS,
    "chrome_devtools": CHROME_DEVTOOLS_TOOLS,
    "browser_all": BROWSER_ALL,
}


def resolve_group_tools(tool_names: list[str]) -> list[str]:
    """Resolve tool group aliases to individual tool names.

    Accepts both aliases ("base", "dev") and individual names ("Read", "Bash").
    """
    result: list[str] = []
    seen: set[str] = set()
    for name in tool_names:
        expanded = TOOL_ALIASES.get(name, [name])
        for tool in expanded:
            if tool not in seen:
                seen.add(tool)
                result.append(tool)
    return result


# ── Dev agent prompts + git helpers (delegated to agents/dev.py) ──
from agents.dev import (
    build_dev_prompt as _build_dev_prompt_fn,
    find_feature_repos,
    snapshot_repo_branches,
    find_changed_repos,
    push_and_create_pr,
)

# ── Runner + tool display (delegated to core/runner.py) ──
from core.runner import run_agent_core as _run_agent_core, ProgressCallback


# ── Biz domain helpers (delegated to core/biz.py) ──
from core.biz import (
    discover_domains,
    load_domain_context,
    repos_path as _biz_repos_path,
)


# Git helpers delegated to agents/dev.py (find_feature_repos, snapshot_repo_branches,
# find_changed_repos, push_and_create_pr imported above)


def _get_chat_lock(chat_id: str) -> threading.Lock:
    """Get or create a per-chat lock."""
    with _chat_locks_meta:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


def _get_cancel_event(chat_id: str) -> threading.Event:
    """Get or create a per-chat cancel event."""
    with _cancel_meta:
        if chat_id not in _cancel_events:
            _cancel_events[chat_id] = threading.Event()
        return _cancel_events[chat_id]


def cancel_agent(chat_id: str) -> bool:
    """Signal a running agent to stop. Returns True if there was a cancel event to set."""
    with _cancel_meta:
        ev = _cancel_events.get(chat_id)
    if ev:
        ev.set()
        logger.info("Cancel requested for chat=%s", chat_id)
        return True
    return False


def init(session_store: SessionStore, agent_session_store: AgentSessionStore, permissions: Permissions | None = None, usage_store=None) -> None:
    """Initialize agent module. Call once at startup."""
    global _session_store, _agent_session_store, _permissions, _usage_store
    _session_store = session_store
    _agent_session_store = agent_session_store
    _permissions = permissions
    _usage_store = usage_store
    import core.mcp
    servers = core.mcp.load()
    logger.info("Agent initialized with %d MCP server(s)", len(servers))


def check_daily_budget(sender_id: str) -> str | None:
    """Check if sender has exceeded daily budget. Returns message if over limit, None otherwise."""
    from agents import get_daily_budget_usd
    limit = get_daily_budget_usd()
    if limit <= 0 or not _usage_store or not sender_id:
        return None
    spent = _usage_store.query_daily(sender_id)
    if spent >= limit:
        return f"今日额度已用完（已用 ${spent:.2f} / 限额 ${limit:.2f}），明天再试吧。"
    return None


from core.mcp import get_servers as _get_mcp_servers


_ROLE_STYLE_HINTS: dict[str, str] = {
    "产品经理": "侧重业务价值、用户体验、需求优先级、ROI 分析，少用代码细节",
    "后端开发": "侧重技术实现、架构设计、API 设计、性能优化，可给代码示例",
    "前端开发": "侧重 UI 交互、组件设计、样式实现、浏览器兼容性，可给代码示例",
    "测试": "侧重测试用例设计、边界条件、回归风险、质量指标，给出具体测试建议",
}


def _build_roles_context(chat_id: str, sender_id: str = "") -> str:
    """Build a roles context string for system prompt injection."""
    if not _permissions:
        return ""
    roles = _permissions.get_roles(chat_id)
    if not roles:
        return ""
    lines = ["\n当前群成员角色："]
    sender_role_name = ""
    for oid, info in roles.items():
        marker = ""
        if oid == sender_id:
            marker = " ← 当前说话人"
            sender_role_name = info["name"]
        lines.append(f"- {info['name']}: {info['desc']}{marker}")

    # Append style hint for the current speaker's role
    if sender_role_name:
        hint = _ROLE_STYLE_HINTS.get(sender_role_name, "")
        if hint:
            lines.append(f"\n请根据当前说话人的角色调整回答侧重点：{hint}")
        else:
            lines.append(f"\n请根据当前说话人「{sender_role_name}」的角色职责调整回答的侧重点和专业程度。")

    return "\n".join(lines) + "\n"


def _build_options(chat_id: str, group: str = "admin", sender_id: str = "") -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a given chat, with group-based tool filtering."""
    s = get_settings()

    # Environment variables for the Claude CLI subprocess
    env = {}
    if s.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = s.anthropic_base_url

    # Session resume
    session_id = _session_store.get(chat_id) if _session_store else None

    # Resolve tools from group config
    group_cfg = _permissions.get_group_config(group) if _permissions else None
    if group_cfg and "all" not in group_cfg.tools:
        allowed_tools = resolve_group_tools(group_cfg.tools)
        # Compute disallowed from individual tool names (not wildcards).
        # MCP wildcards (e.g. mcp__agent-feishu-mcp__*) in disallowed_tools
        # crash the SDK CLI — it blocks the entire MCP server while it's
        # still connected.  Individual MCP tool names are safe to disallow.
        allowed_set = set(allowed_tools)
        disallowed_tools = [
            t for t in _ALL_INDIVIDUAL_TOOLS
            if t not in allowed_set
        ]
    else:
        allowed_tools = TOOLS_ADMIN
        disallowed_tools = []

    # Path guard hook
    hooks = {}
    if group_cfg and group_cfg.paths:
        from core.path_guard import make_path_guard_hook
        base_dir = str(s.biz_base_path) if s.biz_base_path else ""
        path_hook = make_path_guard_hook(group_cfg.paths, base_dir)
        if path_hook:
            hooks["PreToolUse"] = hooks.get("PreToolUse", []) + [path_hook]

    roles_ctx = _build_roles_context(chat_id, sender_id)

    from agents import get_chat_config
    chat_cfg = get_chat_config()

    from agents.prompts import turn_budget_hint
    prompt = build_system_prompt() + OUTPUT_STYLE_GUIDE + roles_ctx
    prompt += _build_feishu_write_hint(sender_id, allowed_tools)
    if group_cfg and group_cfg.paths:
        prompt += f"\n\n你的文件访问范围：{', '.join(group_cfg.paths)}\n请勿尝试访问以上范围外的路径，工具调用会被系统拦截。"
    prompt += turn_budget_hint(chat_cfg.max_turns)

    # ── Context compression: inject summary if session was compressed ──
    if not session_id and _session_store:
        _summary = _session_store.get_summary(chat_id)
        if _summary:
            from core.context_compress import build_summary_section
            prompt += build_summary_section(_summary)
            logger.info("Injecting compressed summary for chat=%s (%d chars)", chat_id, len(_summary))

    mcp = _get_mcp_servers(group, allowed_tools)
    logger.debug(
        "Chat options: group=%s, allowed=%d, disallowed=%d, mcp=%s, model=%s, resume=%s",
        group, len(allowed_tools), len(disallowed_tools), list(mcp.keys()),
        get_effective_model(chat_id), bool(session_id),
    )

    opts = ClaudeAgentOptions(
        model=get_effective_model(chat_id),
        system_prompt=prompt,
        mcp_servers=mcp,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode="bypassPermissions",
        max_turns=chat_cfg.max_turns,
        max_budget_usd=chat_cfg.max_budget_usd,
        env=env,
        hooks=hooks if hooks else {},
        include_partial_messages=True,
    )

    if session_id:
        opts.resume = session_id

    return opts


# ── Generic agent options builder ──

_DEV_WORKFLOW_SERVER_NAME = "dev-workflow"
_DEV_PHASE_TOOL = f"mcp__{_DEV_WORKFLOW_SERVER_NAME}__update_dev_phase"

_VALID_PHASES = {"explore", "planning", "implementing"}


def _make_dev_workflow_server(chat_id: str):
    """Create an in-process MCP server with dev workflow tools.

    The tool handler captures chat_id and session_store via closure,
    so the agent can update phase without knowing system internals.
    """
    @sdk_tool(
        "update_dev_phase",
        "更新开发阶段。在与用户确认阶段切换后调用。"
        "phase 可选值：planning（方案设计）、implementing（编码实现）、explore（探索）。"
        "summary 填写需求摘要或阶段说明。",
        {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["explore", "planning", "implementing"],
                    "description": "目标阶段",
                },
                "summary": {
                    "type": "string",
                    "description": "需求摘要或阶段切换说明",
                },
            },
            "required": ["phase"],
        },
    )
    async def update_dev_phase(args):
        phase = args.get("phase", "")
        summary = args.get("summary", "")
        if phase not in _VALID_PHASES:
            return {
                "content": [{"type": "text", "text": f"无效阶段: {phase}"}],
                "is_error": True,
            }
        if _agent_session_store:
            _agent_session_store.set_phase(chat_id, phase)
            if summary and phase == "planning":
                _agent_session_store.set_requirement(chat_id, summary)
            elif summary and phase == "implementing":
                _agent_session_store.set_plan(chat_id, summary)
        logger.info("Dev phase update via tool: chat=%s phase=%s summary=%s", chat_id, phase, summary[:80])
        return {"content": [{"type": "text", "text": f"阶段已更新为 {phase}。"}]}

    return create_sdk_mcp_server(
        name=_DEV_WORKFLOW_SERVER_NAME,
        version="1.0.0",
        tools=[update_dev_phase],
    )


def _build_agent_options(chat_id: str, agent_cfg, agent_state, sender_id: str = "", group: str = "admin") -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions from AgentConfig + AgentState.

    Works for any registered agent (dev, ops, knowledge, etc.).
    Group is used for path guard enforcement (tool set comes from agent_cfg).
    """
    from agents import AgentConfig
    s = get_settings()

    env = {}
    if s.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = s.anthropic_base_url

    domains = agent_state.domains if agent_state else []
    requirement = agent_state.requirement if agent_state else ""
    phase = agent_state.phase if agent_state else ""
    plan_summary = agent_state.plan_summary if agent_state else ""

    # Build context for prompt builder
    ctx: dict = {
        "requirement": requirement,
        "phase": phase,
        "plan_summary": plan_summary,
        "roles_context": _build_roles_context(chat_id, sender_id),
    }

    cwd = None
    if agent_cfg.requires_domain and domains:
        domain_ctx = load_domain_context(domains)
        ctx.update(domain_ctx)
        if not agent_cfg.include_claude_md:
            ctx["claude_md"] = ""
        if len(domains) > 1:
            cwd = ctx.get("biz_base")
        else:
            cwd = ctx.get("repos_path")

        # Worktree isolation: create per-chat worktree for agents that need it
        if getattr(agent_cfg, "needs_isolation", False) and len(domains) == 1:
            from core.worktree import get_worktree_path, create_worktrees, check_dirty_state, touch_worktree
            wt_path = get_worktree_path(domains[0], chat_id)
            if not wt_path:
                wt_path = create_worktrees(domains[0], chat_id)
            else:
                touch_worktree(domains[0], chat_id)
            if wt_path:
                cwd = wt_path
                ctx["repos_path"] = wt_path
                ctx["worktree_active"] = True
                dirty = check_dirty_state(domains[0], chat_id)
                if dirty:
                    ctx["dirty_warning"] = "注意：以下仓库有未提交的修改：" + ", ".join(d["repo"] for d in dirty)
        elif getattr(agent_cfg, "needs_isolation", False) and len(domains) > 1:
            logger.warning("Worktree isolation skipped for multi-domain session: %s", domains)

    # Build system prompt via agent's prompt builder
    if agent_cfg.build_prompt:
        prompt_text = agent_cfg.build_prompt(ctx)
    else:
        prompt_text = build_system_prompt() + ctx.get("roles_context", "")
    prompt_text += OUTPUT_STYLE_GUIDE
    group_cfg_for_prompt = _permissions.get_group_config(group) if _permissions else None
    if group_cfg_for_prompt and group_cfg_for_prompt.paths:
        prompt_text += f"\n\n你的文件访问范围：{', '.join(group_cfg_for_prompt.paths)}\n请勿尝试访问以上范围外的路径，工具调用会被系统拦截。"

    # Tools, budget, turns, effort — phase-aware
    allowed = agent_cfg.tools

    if phase in ("explore", "planning") and agent_cfg.explore_max_turns > 0:
        max_turns = agent_cfg.explore_max_turns
    else:
        max_turns = agent_cfg.max_turns

    from agents.prompts import turn_budget_hint
    prompt_text += turn_budget_hint(max_turns)

    if phase in ("explore", "planning") and agent_cfg.explore_max_budget_usd > 0:
        budget = agent_cfg.explore_max_budget_usd
    else:
        budget = agent_cfg.max_budget_usd if agent_cfg.max_budget_usd > 0 else 0.0

    # Explore/planning uses lighter reasoning; implementing gets full depth
    effort = "medium" if phase in ("explore", "planning") else None
    disallowed: list[str] = []
    hooks = agent_cfg.hooks

    # Add path guard hook based on group config
    if not hooks:
        hooks = {}
    group_cfg = _permissions.get_group_config(group) if _permissions else None
    if group_cfg and group_cfg.paths:
        from core.path_guard import make_path_guard_hook
        s2 = get_settings()
        base_dir = str(s2.biz_base_path) if s2.biz_base_path else ""
        path_hook = make_path_guard_hook(group_cfg.paths, base_dir)
        if path_hook:
            hooks["PreToolUse"] = hooks.get("PreToolUse", []) + [path_hook]

    prompt_text += _build_feishu_write_hint(sender_id, allowed)

    # Build MCP servers — inject dev workflow tool for dev agent
    mcp = {**_get_mcp_servers(group, allowed), **agent_cfg.mcp_servers}
    if agent_cfg.name == "dev":
        mcp[_DEV_WORKFLOW_SERVER_NAME] = _make_dev_workflow_server(chat_id)
        allowed = list(allowed) + [_DEV_PHASE_TOOL]

    # Log key config for debugging
    has_feishu_write = any("create-doc" in t for t in allowed)
    logger.info(
        "Agent[%s] options: group=%s, tools=%d, feishu_write=%s, mcp=%s, model=%s, max_turns=%s, budget=%s, phase=%s, effort=%s",
        agent_cfg.name, group, len(allowed), has_feishu_write,
        list(mcp.keys()), get_effective_model(chat_id, agent_cfg),
        max_turns, f"${budget:.2f}" if budget > 0 else "none",
        phase or "none", effort or "default",
    )
    logger.debug(
        "Agent[%s] allowed_tools: %s", agent_cfg.name, allowed,
    )
    logger.debug(
        "Agent[%s] prompt length: %d chars, domains=%s, cwd=%s, resume=%s",
        agent_cfg.name, len(prompt_text), domains, cwd,
        bool(agent_state.session_id if agent_state else None),
    )

    opts = ClaudeAgentOptions(
        model=get_effective_model(chat_id, agent_cfg),
        system_prompt=prompt_text,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp,
        env=env,
        hooks=hooks,
        include_partial_messages=True,
        effort=effort,
    )

    if budget > 0:
        opts.max_budget_usd = budget

    if cwd:
        opts.cwd = cwd

    # Session resume
    session_id = agent_state.session_id if agent_state else None
    if session_id:
        opts.resume = session_id

    return opts


# ── Core agent loop is in core/runner.py (_run_agent_core imported above) ──


# ── Normal chat ──

async def _run_agent(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    group: str = "admin",
    sender_id: str = "",
    cancel_event: threading.Event | None = None,
    status_suffix: str = "",
) -> str:
    """Run the normal chat agent loop."""
    options = _build_options(chat_id, group=group, sender_id=sender_id)
    result = await _run_agent_core(
        text, options, on_progress, sender_id, chat_id,
        cancel_event=cancel_event,
        status_suffix=status_suffix,
    )
    if result.session_id and _session_store:
        _session_store.set(chat_id, result.session_id)

    # ── Trigger context compression if input_tokens exceed threshold ──
    if _session_store and result.input_tokens > 0:
        from agents import get_chat_config as _gcc
        from core.context_compress import should_compress, compress_context_async
        _cc = _gcc()
        if should_compress(result.input_tokens, _cc.compress_threshold):
            logger.info(
                "[Compress] Triggering: chat=%s, input_tokens=%d, threshold=%d",
                chat_id, result.input_tokens, _cc.compress_threshold,
            )
            compress_context_async(chat_id, text, result.text, _session_store, model=_cc.compress_model)

    if _usage_store:
        try:
            _usage_store.log(
                chat_id=chat_id,
                sender_id=sender_id,
                agent_name="chat",
                model=get_effective_model(chat_id),
                result=result,
            )
        except Exception:
            logger.warning("Failed to log usage for chat=%s", chat_id, exc_info=True)

    return result.text


def run(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    group: str = "admin",
    sender_id: str = "",
    status_suffix: str = "",
) -> str:
    """Synchronous entry point for normal chat.

    Safe to call from a worker thread.
    Acquires a per-chat lock to serialize concurrent requests.
    """
    # Route to lite provider if configured
    if get_settings().llm_provider == "lite":
        from providers.lite_agent import run as lite_run
        is_admin = (group == "admin")
        return lite_run(chat_id, text, on_progress, is_admin, sender_id)

    cancel_event = _get_cancel_event(chat_id)
    cancel_event.clear()

    lock = _get_chat_lock(chat_id)
    if not lock.acquire(timeout=120):
        raise TimeoutError("该会话正忙，请稍后再试")
    try:
        return asyncio.run(_run_agent(
            chat_id, text, on_progress,
            group=group, sender_id=sender_id,
            cancel_event=cancel_event,
            status_suffix=status_suffix,
        ))
    finally:
        lock.release()


# ── Generic agent session ──

async def _run_agent_session_async(
    chat_id: str,
    text: str,
    agent_name: str,
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
    group: str = "admin",
    cancel_event: threading.Event | None = None,
    status_suffix: str = "",
) -> str:
    """Run an agent session (generic, works for any registered agent)."""
    from agents import get_agent

    agent_cfg = get_agent(agent_name)
    if not agent_cfg:
        return f"未找到 agent: {agent_name}"

    agent_state = _agent_session_store.get(chat_id) if _agent_session_store else None

    # Per-agent session TTL: expire session_id if idle too long
    if (agent_state and agent_state.session_id and agent_cfg.session_ttl > 0):
        idle = time.time() - agent_state.started_at
        if idle > agent_cfg.session_ttl:
            logger.info(
                "Agent session expired: chat=%s agent=%s idle=%.0fs ttl=%ds, starting fresh",
                chat_id, agent_name, idle, agent_cfg.session_ttl,
            )
            _agent_session_store.clear_session_id(chat_id)
            agent_state.session_id = None

    options = _build_agent_options(chat_id, agent_cfg, agent_state, sender_id=sender_id, group=group)

    result = await _run_agent_core(
        text, options, on_progress, sender_id, chat_id,
        cancel_event=cancel_event,
        status_suffix=status_suffix,
    )

    if result.session_id and _agent_session_store:
        _agent_session_store.set_session_id(chat_id, result.session_id)

    if _usage_store:
        try:
            domain = agent_state.domain if agent_state else ""
            _usage_store.log(
                chat_id=chat_id,
                sender_id=sender_id,
                agent_name=agent_name,
                domain=domain,
                model=get_effective_model(chat_id, agent_cfg),
                result=result,
            )
        except Exception:
            logger.warning("Failed to log usage for chat=%s agent=%s", chat_id, agent_name, exc_info=True)

    return result.text


def run_agent_session(
    chat_id: str,
    text: str,
    agent_name: str,
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
    group: str = "admin",
    status_suffix: str = "",
) -> str:
    """Synchronous entry point for any agent session.

    Same locking as normal chat (shared per chat_id).
    """
    cancel_event = _get_cancel_event(chat_id)
    cancel_event.clear()

    lock = _get_chat_lock(chat_id)
    if not lock.acquire(timeout=300):  # agent tasks can be longer
        raise TimeoutError("该会话正忙，请稍后再试")
    try:
        return asyncio.run(_run_agent_session_async(
            chat_id, text, agent_name, on_progress,
            sender_id=sender_id, group=group,
            cancel_event=cancel_event,
            status_suffix=status_suffix,
        ))
    finally:
        lock.release()


# ── Dev mode (thin wrapper) ──

def run_dev(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
    group: str = "admin",
    status_suffix: str = "",
) -> str:
    """Synchronous entry point for dev mode. Delegates to run_agent_session."""
    # Route to lite provider if configured
    if get_settings().llm_provider == "lite":
        from providers.lite_agent import run_dev as lite_run_dev
        return lite_run_dev(chat_id, text, on_progress, sender_id)

    return run_agent_session(chat_id, text, "dev", on_progress, sender_id, group=group, status_suffix=status_suffix)


# ── Orchestrator (role agent) ──
# Uses SDK-native AgentDefinition + Task tool for sub-agent delegation.
# The SDK manages sub-agent subprocess lifecycle internally — no nested query() calls.

async def _run_orchestrator_async(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    group: str = "admin",
    sender_id: str = "",
    cancel_event: threading.Event | None = None,
    status_suffix: str = "",
) -> str:
    """Run the orchestrator agent loop."""
    options = _build_orchestrator_options(chat_id, sender_id, group, user_message=text)
    result = await _run_agent_core(
        text, options, on_progress, sender_id, chat_id,
        cancel_event=cancel_event,
        status_suffix=status_suffix or "· Role",
    )

    # Save session for conversation continuity
    if result.session_id and _session_store:
        _session_store.set(chat_id, result.session_id)

    # ── Trigger context compression if input_tokens exceed threshold ──
    if _session_store and result.input_tokens > 0:
        from agents import get_chat_config as _gcc
        from core.context_compress import should_compress, compress_context_async
        _cc = _gcc()
        if should_compress(result.input_tokens, _cc.compress_threshold):
            logger.info(
                "[Compress] Triggering: orchestrator chat=%s, input_tokens=%d, threshold=%d",
                chat_id, result.input_tokens, _cc.compress_threshold,
            )
            compress_context_async(chat_id, text, result.text, _session_store, model=_cc.compress_model)

    if _usage_store:
        try:
            _usage_store.log(
                chat_id=chat_id,
                sender_id=sender_id,
                agent_name="role",
                model=get_effective_model(chat_id, _get_role_agent_cfg()),
                result=result,
            )
        except Exception:
            logger.warning("Failed to log usage for orchestrator chat=%s", chat_id, exc_info=True)

    return result.text


def _get_role_agent_cfg():
    """Get the role agent config (lazy import to avoid circular deps)."""
    from agents import get_agent
    return get_agent("role")


def _build_domain_info() -> str:
    """Build domain info string with paths for prompts."""
    domains = discover_domains()
    if not domains:
        return ""
    lines = ["可用项目域："]
    for d in domains:
        rp = _biz_repos_path(d)
        lines.append(f"- {d}: 代码 {rp}")
    return "\n".join(lines)


def _load_orchestrator_knowledge() -> dict:
    """Load knowledge context for the orchestrator (role agent).

    Returns dict with keys: knowledge_index, knowledge_overview, db_clusters.
    Extracted from _build_orchestrator_options to keep it focused.
    """
    from core.biz import load_knowledge_index, load_knowledge_overview, _resolve_biz_base

    all_domains = discover_domains()
    ki_parts: list[str] = []
    ko_parts: list[str] = []
    db_clusters = ""

    for d in all_domains:
        ki = load_knowledge_index(d)
        if ki:
            ki_parts.append(ki if len(all_domains) == 1 else f"[{d}]\n{ki}")
        ko = load_knowledge_overview(d)
        if ko:
            ko_parts.append(ko if len(all_domains) == 1 else f"[{d}]\n{ko}")
        # Load database-query.md so role agent can answer DB queries directly
        db_path = _resolve_biz_base() / d / "knowledge" / "database-query.md"
        if db_path.exists():
            content = db_path.read_text(encoding="utf-8").strip()
            if content:
                db_clusters = content

    return {
        "knowledge_index": "\n\n".join(ki_parts) if ki_parts else None,
        "knowledge_overview": "\n\n".join(ko_parts) if ko_parts else None,
        "db_clusters": db_clusters or None,
    }


def _build_sub_agent_definitions(mcp_servers: dict | None = None) -> dict:
    """Build AgentDefinition objects from the agent registry.

    Inspired by Claude Code's coordinator pattern: sub-agents are defined
    declaratively and the coordinator only has the Task tool for delegation.

    Each registered agent (dev, ask, ops) is converted to an AgentDefinition
    with its prompt, tools, and model from config.yaml. Adding a new agent
    only requires registering it — no changes to this function.
    """
    from claude_agent_sdk import AgentDefinition
    from agents import list_agents, get_agent
    from agents.prompts import SUB_AGENT_TURN_LIMIT

    domain_info = _build_domain_info()
    base_ctx = load_domain_context([], include_base=True).get("base_context", "")
    bg = f"公司背景：\n{base_ctx}\n\n" if base_ctx else ""

    # Convert MCP servers dict to SDK format
    mcp_list = None
    if mcp_servers:
        mcp_list = [{name: config} for name, config in mcp_servers.items()]

    # Sub-agent prompt/tool overrides (orchestrator context differs from standalone)
    _SUB_AGENT_OVERRIDES: dict[str, dict] = {
        "dev": {
            "tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
        },
        "ask": {
            "tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
        },
        "ops": {
            "tools": ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"],
        },
    }

    definitions = {}
    for cfg in list_agents():
        # Skip the role agent itself (it's the coordinator, not a sub-agent)
        if cfg.name == "role":
            continue

        # Build sub-agent prompt from its build_prompt if available
        if cfg.build_prompt:
            sub_prompt = cfg.build_prompt({
                "base_context": base_ctx,
                "domains": [],
                "has_knowledge": False,
                "repos_path": "",
                "roles_context": "",
            })
        else:
            sub_prompt = f"{bg}你是 {cfg.display_name} 助手。\n\n{domain_info}"

        sub_prompt += f"\n\n{SUB_AGENT_TURN_LIMIT}\n用中文回复。"

        # Tools: use override if defined, otherwise agent's own tools
        overrides = _SUB_AGENT_OVERRIDES.get(cfg.name, {})
        tools = overrides.get("tools", cfg.tools)

        # Model: read from agent config (set via config.yaml), fallback to sonnet
        model = resolve_model(cfg.model) if cfg.model else "claude-sonnet-4-6"
        model_alias = cfg.model or "sonnet"

        definitions[cfg.name] = AgentDefinition(
            description=f"{cfg.display_name}：{cfg.description}",
            prompt=sub_prompt,
            tools=tools,
            model=model_alias,
            mcpServers=mcp_list,
        )

    return definitions


def _build_orchestrator_options(
    chat_id: str,
    sender_id: str,
    group: str,
    user_message: str = "",
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for the orchestrator (role agent).

    Architecture aligned with Claude Code's Coordinator Mode:
    - Orchestrator has ONLY the Task tool (pure router)
    - Sub-agents are built from the agent registry (data-driven)
    - MCP servers are filtered by query relevance (token optimization)
    - Knowledge is injected into orchestrator prompt (not sub-agents)
    """
    s = get_settings()
    role_cfg = _get_role_agent_cfg()

    env = {}
    if s.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = s.anthropic_base_url

    # ── MCP servers for sub-agents (filtered by query relevance) ──
    if user_message:
        from core.mcp import get_servers_for_query
        mcp_for_agents = get_servers_for_query(group, TOOLS_ADMIN, user_message)
    else:
        mcp_for_agents = _get_mcp_servers(group, TOOLS_ADMIN)

    # ── Sub-agent definitions (auto-generated from registry) ──
    agents = _build_sub_agent_definitions(mcp_servers=mcp_for_agents)
    allowed_tools = ["Task"]

    # ── Log config ──
    agent_names = list(agents.keys())
    agent_mcp = {n: bool(d.mcpServers) for n, d in agents.items()}
    logger.info(
        "Orchestrator options: group=%s, tools=%s, agents=%s, agent_mcp=%s, model=%s",
        group, allowed_tools, agent_names, agent_mcp,
        get_effective_model(chat_id, role_cfg),
    )
    for name, defn in agents.items():
        mcp_names = [list(s.keys())[0] if isinstance(s, dict) else s for s in (defn.mcpServers or [])]
        logger.debug("  sub-agent[%s]: tools=%s, model=%s, mcp=%s", name, defn.tools, defn.model, mcp_names)

    # ── System prompt (with knowledge injection) ──
    base_domain_ctx = load_domain_context([], include_base=True)
    knowledge = _load_orchestrator_knowledge()

    ctx = {
        "base_context": base_domain_ctx.get("base_context", ""),
        "roles_context": _build_roles_context(chat_id, sender_id),
        "domain_info": _build_domain_info(),
        **knowledge,
    }
    prompt = role_cfg.build_prompt(ctx) if role_cfg and role_cfg.build_prompt else ""
    prompt += OUTPUT_STYLE_GUIDE

    # ── Session resume + context compression ──
    session_id = _session_store.get(chat_id) if _session_store else None
    if not session_id and _session_store:
        _summary = _session_store.get_summary(chat_id)
        if _summary:
            from core.context_compress import build_summary_section
            prompt += build_summary_section(_summary)
            logger.info("Injecting compressed summary for orchestrator chat=%s (%d chars)", chat_id, len(_summary))

    # ── Build options ──
    opts = ClaudeAgentOptions(
        model=get_effective_model(chat_id, role_cfg),
        system_prompt=prompt,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=role_cfg.max_turns if role_cfg else 50,
        max_budget_usd=role_cfg.max_budget_usd if role_cfg and role_cfg.max_budget_usd > 0 else 3.0,
        agents=agents,
        env=env,
        include_partial_messages=True,
    )

    if session_id:
        opts.resume = session_id

    return opts


def run_orchestrator(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    group: str = "admin",
    sender_id: str = "",
    status_suffix: str = "",
) -> str:
    """Synchronous entry point for orchestrator mode.

    Same locking pattern as run() — one request per chat at a time.
    """
    cancel_event = _get_cancel_event(chat_id)
    cancel_event.clear()

    lock = _get_chat_lock(chat_id)
    if not lock.acquire(timeout=300):
        raise TimeoutError("该会话正忙，请稍后再试")
    try:
        return asyncio.run(_run_orchestrator_async(
            chat_id, text, on_progress,
            group=group, sender_id=sender_id,
            cancel_event=cancel_event,
            status_suffix=status_suffix,
        ))
    finally:
        lock.release()
