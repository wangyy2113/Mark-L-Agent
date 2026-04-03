"""Role agent — orchestrator that routes to sub-agents or answers directly.

Uses SDK-native AgentDefinition + Task tool for delegation.
Sub-agents are defined as AgentDefinition objects and passed to the SDK,
which manages their subprocess lifecycle internally.
"""

from agents import AgentConfig, register

from tools import PROFILE_ORCHESTRATOR


ROLE_IDENTITY = """\
你是一个技术团队助手（Role Agent），运行在用户的本地机器上。
你可以直接回答问题，也可以通过 Task 工具将任务委托给专业的子 Agent 执行。"""

ROUTING_GUIDE = """\
路由决策指南（严格遵循）：

你是一个纯路由器。你唯一的工具是 Task，用来委托子 Agent 执行任务。
不需要使用工具的简单问题可以直接回答。

**直接回答**（不需要工具，用你自己的知识回答）：
- 一般性技术问题、概念解释、方案讨论
- 日常对话、闲聊
- 业务知识库中已有的内容

**委托给 dev**（代码修改、文档创建）：
- 修改代码、调试、Bug 修复
- 执行 Shell 命令（构建、测试等）
- Git 操作（查看 commit、提交、PR）
- 创建/编辑飞书文档

**委托给 ask**（代码/文档分析、飞书文档/项目查询）：
- 查看/分析项目代码、架构、模块职责
- 查找功能实现细节、业务逻辑
- 搜索/查看飞书文档和知识库
- 查询多维表格数据

**委托给 ops**（监控和日志分析）：
- 查看监控指标（CPU、内存、QPS、延迟）
- 查询线上日志、排查故障"""

DELEGATE_USAGE_GUIDE = """\
Task 工具使用指南：

1. **委托前**：
   - 明确任务描述，让子 Agent 能独立完成
   - 如果用户的请求不够清楚，先向用户确认再委托
   - 任务描述中必须包含项目域路径（从"可用项目域"中选取）

2. **委托后**：
   - 审阅子 Agent 的返回结果
   - 直接将结果转述给用户，可适当补充上下文
   - 如果结果不完整，可以再次委托或自己补充

3. **禁止**：
   - 不要对同一个任务重复委托
   - 不要把简单问题委托出去，浪费资源"""


def build_role_prompt(ctx: dict) -> str:
    """Build the role agent system prompt from context."""
    sections = [ROLE_IDENTITY]

    # Company/org background from biz/_base/
    base_ctx = ctx.get("base_context", "")
    if base_ctx:
        sections.append(f"公司背景：\n{base_ctx}")

    # Available domains with paths
    domain_info = ctx.get("domain_info", "")
    if domain_info:
        sections.append(domain_info)

    # Roles context (chat member roles)
    roles_ctx = ctx.get("roles_context", "")
    if roles_ctx:
        sections.append(roles_ctx)

    # Knowledge overview (system-level summary from knowledge/overview.md)
    knowledge_overview = ctx.get("knowledge_overview")
    if knowledge_overview:
        sections.append(f"业务知识概览：\n{knowledge_overview}")

    # Knowledge index (file listing from knowledge/index.md)
    knowledge_index = ctx.get("knowledge_index")
    if knowledge_index:
        sections.append(f"业务知识库索引：\n{knowledge_index}")

    # DB clusters quick-reference (always inject if present)
    db_clusters = ctx.get("db_clusters")
    if db_clusters:
        sections.append(
            "数据库集群参考（查询线上数据时直接使用，无需再委托查文档）：\n"
            + db_clusters
        )

    sections.append(ROUTING_GUIDE)
    sections.append(DELEGATE_USAGE_GUIDE)

    # Available skills
    from core.skills import build_skills_prompt
    skills_section = build_skills_prompt()
    if skills_section:
        sections.append(skills_section)

    sections.append(
        "重要：你只有 Task 工具，没有其他工具。任何需要搜索、读取、执行的操作都必须通过 Task 委托给子 Agent。\n"
        "用中文回复。"
    )

    sections.append("用中文回复。")

    return "\n\n".join(sections)


ROLE_AGENT = AgentConfig(
    name="role",
    display_name="Role",
    description="智能路由，自主判断回答问题或委托子 Agent",
    command="/role",
    tools=PROFILE_ORCHESTRATOR,
    requires_domain=False,
    has_explore_mode=False,
    needs_isolation=False,
    max_turns=50,
    max_budget_usd=3.0,
    model="sonnet",
    build_prompt=build_role_prompt,
)

register(ROLE_AGENT)
