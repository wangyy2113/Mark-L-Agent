"""Knowledge agent — read-only domain Q&A based on code and docs."""

from agents import AgentConfig, register
from agents.prompts import (
    TOOL_MAPPING, NO_BASH_SEARCH, PARALLEL_CALLS, NO_REPEAT_READ,
    CITE_PATHS, MIN_TOOL_CALLS, SEARCH_LIMIT, FOCUS_RELATED,
    READONLY, NO_BASH_TASK_TODO,
    READ_INDEX_FIRST, VERIFY_STALE, MULTI_DOMAIN_HINT,
    SUB_AGENT_TURN_LIMIT,
)
from tools import PROFILE_READONLY


KB_ROLE = """\
你是一个代码问答助手，基于项目代码和文档回答技术与业务问题。

你可以：
1. 阅读项目代码，理解架构、模块职责、数据流
2. 搜索飞书文档和知识库，补充上下文
3. 搜索网页获取外部参考资料"""

KB_PRINCIPLES = "\n".join([
    "工作原则：",
    CITE_PATHS,
    FOCUS_RELATED,
    MIN_TOOL_CALLS,
    PARALLEL_CALLS,
    NO_REPEAT_READ,
    SEARCH_LIMIT,
    SUB_AGENT_TURN_LIMIT,
    READONLY,
])

KB_STRATEGY_WITH_KNOWLEDGE = "\n".join([
    "查询策略（有知识库）：",
    READ_INDEX_FIRST,
    "- 需要更多细节时读 details/ 或回溯源码",
    VERIFY_STALE,
])

KB_STRATEGY_FALLBACK = """\
查询策略：
- 先从 CLAUDE.md、README.md 建立全貌，再按需深入
- 使用 Glob/Grep/Read 在代码仓库中搜索相关内容"""

KB_TOOL_RULES = "\n".join([
    "工具使用规则（严格遵守）：",
    NO_BASH_TASK_TODO,
    NO_BASH_SEARCH,
    TOOL_MAPPING,
    "- 搜索飞书文档 → 飞书 MCP 工具",
    "- 搜索网页 → WebSearch / WebFetch",
])


def build_kb_prompt(ctx: dict) -> str:
    """Build the knowledge agent system prompt from context."""
    rp = ctx.get("repos_path", "")
    has_kb = ctx.get("has_knowledge", False)
    domains = ctx.get("domains", [])

    sections = [KB_ROLE]

    # Company background
    base_ctx = ctx.get("base_context", "")
    if base_ctx:
        sections.append(f"公司背景：\n{base_ctx}")

    # Domain info (repos, prompt)
    r_paths = ctx.get("repos_paths", [])
    if r_paths:
        if len(domains) > 1:
            path_lines = []
            for i, d in enumerate(domains):
                rr = r_paths[i] if i < len(r_paths) else ""
                path_lines.append(f"- [{d}] 代码：{rr}")
            domain_info = "项目信息：\n" + "\n".join(path_lines)
        else:
            domain_info = f"项目信息：\n- 代码库根目录：{rp}"
        domain_prompt = ctx.get("domain_prompt", "")
        if domain_prompt:
            domain_info += f"\n{domain_prompt}"
        sections.append(domain_info)

    # Business context
    context = ctx.get("context", "")
    if context:
        sections.append(f"业务上下文：\n{context}")

    # System overview (from knowledge/)
    overview = ctx.get("knowledge_overview")
    if overview:
        sections.append(f"系统概览：\n{overview}")

    # CLAUDE.md
    claude_md = ctx.get("claude_md", "")
    if claude_md:
        sections.append(claude_md)

    # Query strategy: knowledge-routed vs fallback
    sections.append(KB_STRATEGY_WITH_KNOWLEDGE if has_kb else KB_STRATEGY_FALLBACK)

    sections.append(KB_PRINCIPLES)

    # Multi-domain hint
    if len(domains) > 1:
        sections.append(MULTI_DOMAIN_HINT)

    sections.append(KB_TOOL_RULES)
    sections.append("用中文回复。")

    prompt = "\n\n".join(sections)
    roles_ctx = ctx.get("roles_context", "")
    if roles_ctx:
        prompt += roles_ctx

    return prompt


KB_AGENT = AgentConfig(
    name="ask",
    display_name="Ask",
    description="基于项目代码和文档回答技术/业务问题（只读）",
    command="/ask",
    model="sonnet",
    tools=PROFILE_READONLY,
    requires_domain=True,
    include_repos=True,
    include_claude_md=True,
    max_turns=20,
    explore_max_turns=20,
    has_explore_mode=False,
    max_budget_usd=0.80,
    session_ttl=3600,  # 1h — KB questions are mostly independent
    build_prompt=build_kb_prompt,
)

register(KB_AGENT)
