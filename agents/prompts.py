"""Shared prompt building blocks for agent system prompts.

Atomic rules that agents pick and compose. Each rule is a single markdown
bullet point (starting with "- ") so it can be embedded directly into
prompt sections.

Usage::

    from agents.prompts import TOOL_MAPPING, NO_BASH_SEARCH, ...

    MY_TOOL_RULES = f\"\"\"\\
    工具使用规则：
    {TOOL_MAPPING}
    {NO_BASH_SEARCH}
    {BASH_ONLY_FOR}
    \"\"\"
"""

# ── Tool mapping (for code-exploring agents) ──

TOOL_MAPPING = """\
- 查找文件 → Glob（如 `**/*.java`、`**/pom.xml`）
- 搜索代码内容 → Grep（支持正则，如 `BlackList|blacklist`）
- 读取文件 → Read
- 查看目录结构 → Glob（如 `*/`、`src/**/`）"""

NO_BASH_SEARCH = "- **禁止**用 Bash 执行 find、grep、cat、head、tail、ls 命令"

PARALLEL_CALLS = (
    "- 每轮尽量**并行调用多个工具**"
    "（Glob/Grep/Read 可以同时调用多个），减少往返次数"
)

NO_REPEAT_READ = "- 同一个文件不要重复读取"

CITE_PATHS = "- 回答时引用具体文件路径和代码位置，不凭空推断"

# ── Efficiency (exploration / Q&A scenarios) ──

MIN_TOOL_CALLS = (
    "- 目标是用**最少的工具调用**回答问题，"
    "找到关键代码即可回答，**避免地毯式扫描**"
)

SEARCH_LIMIT = "- 如果搜索了 3-5 轮仍未找到相关代码，应基于已有信息回答，不要无限搜索"

FOCUS_RELATED = "- 聚焦于与用户问题**直接相关**的模块，不要发散到无关领域"

# ── Access control ──

READONLY = "- **不要修改任何文件**，只做读取和分析"

NO_BASH_TASK_TODO = "- **禁止**使用 Bash、Task、TodoWrite 工具 — 你只有只读工具"

BASH_ONLY_FOR = "- Bash **仅**用于：git 命令、mvn/gradle 构建、运行脚本"

# ── Knowledge routing ──

READ_INDEX_FIRST = (
    "- 有知识库时，**先读 knowledge/index.md** 确定推荐文件，"
    "再按推荐读取 modules/、flows/、details/ 下的文件"
)

VERIFY_STALE = "- 知识文件可能过期，关键结论需**回溯源码验证**"

MULTI_DOMAIN_HINT = "- 多域场景：每个域的内容已按 `[域名]` 标签标注，注意区分不同域的上下文"

# ── Sub-agent turn limit (prompt-level, for orchestrator sub-agents) ──

SUB_AGENT_TURN_LIMIT = (
    "- 如果 15 轮工具调用后仍未完成任务，"
    "**停止搜索**，基于已获取的信息给出最佳回答"
)


# ── Turn budget hint (dynamic, injected by runner) ──

def turn_budget_hint(max_turns: int | None) -> str:
    """Generate a prompt hint about the available turn budget.

    The SDK's max_turns is a hard cutoff that Claude doesn't know about.
    Telling Claude the budget lets it plan steps and avoid getting cut off
    mid-task.  Returns empty string if max_turns is not set.
    """
    if not max_turns or max_turns <= 0:
        return ""
    warn_at = max(max_turns - 5, max_turns * 2 // 3)
    return (
        f"\n\n资源限制：本次最多 {max_turns} 轮工具调用。"
        f"超过 {warn_at} 轮后必须停止探索，基于已有信息输出结论。"
        "优先 Grep 定位关键行再 Read 片段，避免 Read 整个大文件。"
        "如果任务复杂，先给出方案摘要，确认后再逐步实施。"
    )

