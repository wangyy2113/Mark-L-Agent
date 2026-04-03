"""Ops agent — monitoring queries, log analysis, incident investigation."""

from agents import AgentConfig, register
from tools import compose_tools
from tools.base import TOOLS as BASE_TOOLS
from tools.ops import TOOLS as OPS_TOOLS, MCP_SERVERS as OPS_MCP


OPS_PROMPT = """\
你是一个运维问题分析助手，帮助团队排查线上问题、分析日志、查看监控指标。

你可以：
1. 查询 Prometheus 监控指标（CPU、内存、QPS、延迟、错误率等）
2. 查询阿里云 SLS 日志（支持自然语言转 SQL、日志探索、日志对比）
3. 执行命令查询日志（grep、jq、curl 等）
4. 搜索网页获取错误码、异常信息的解释

{project_info}

可用工具说明：
- query_prometheus: 查询自建 Prometheus/Promxy，支持 PromQL，支持 now-1h 等相对时间
- sls_execute_sql: 直接执行 SLS SQL 查询
- sls_text_to_sql: 用自然语言描述需求，自动生成 SLS SQL
- sls_log_explore: 日志探索分析，发现异常模式
- sls_log_compare: 对比两个时间段的日志差异
- cms_text_to_promql: 用自然语言描述需求，自动生成 PromQL（可配合 query_prometheus 执行）

工作原则：
- 先明确问题现象（时间范围、影响范围、错误信息）
- 从监控和日志入手，逐步缩小排查范围
- 结合代码逻辑分析根因，不仅仅描述现象
- 给出明确的结论和建议的修复方向
- 如果信息不足，主动提问获取更多上下文

安全规则：
- 只执行只读查询命令，不要修改线上配置
- 不要重启服务或执行可能影响线上的操作
- 涉及敏感信息（密码、token）时脱敏处理

用中文回复。问题分析时使用结构化格式：现象 → 分析 → 结论 → 建议。"""


def build_ops_prompt(ctx: dict) -> str:
    """Build the ops agent system prompt from context."""
    rp = ctx.get("repos_path", "")

    if rp:
        project_info = f"关联项目：\n- 代码库：{rp}\n"
        domain_prompt = ctx.get("domain_prompt", "")
        if domain_prompt:
            project_info += f"\n{domain_prompt}"
    else:
        project_info = ""

    prompt = OPS_PROMPT.format(project_info=project_info)
    roles_ctx = ctx.get("roles_context", "")
    if roles_ctx:
        prompt += roles_ctx

    return prompt


OPS_AGENT = AgentConfig(
    name="ops",
    display_name="Ops",
    description="监控查询、日志分析、故障定位",
    command="/ops",
    tools=compose_tools(BASE_TOOLS, ["Bash"], OPS_TOOLS),
    mcp_servers=dict(OPS_MCP) if OPS_MCP else {},
    requires_domain=False,
    include_repos=False,
    include_claude_md=False,
    max_turns=30,
    explore_max_turns=15,
    has_explore_mode=False,
    max_budget_usd=1.00,
    build_prompt=build_ops_prompt,
)

register(OPS_AGENT)
