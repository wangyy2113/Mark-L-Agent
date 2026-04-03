"""Dev agent — domain-scoped coding assistant with git workflow."""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from agents import AgentConfig, register
from agents.prompts import (
    TOOL_MAPPING,
    NO_BASH_SEARCH,
    PARALLEL_CALLS,
    NO_REPEAT_READ,
    CITE_PATHS,
    MIN_TOOL_CALLS,
    FOCUS_RELATED,
    BASH_ONLY_FOR,
)
from tools import PROFILE_READWRITE
from tools.dev import BASH_HOOK
from core.biz import repos_path, load_domain_prompt, load_claude_md
from core.worktree import detect_default_branch

logger = logging.getLogger(__name__)


# ── Dev prompt ──
# Single workflow document — phases are LLM-managed, not system-enforced.
# Tools are the same in all phases; HARD-GATE prevents premature code changes.

DEV_PROMPT_BASE = """\
你是一个研发助手，运行在开发者的本地机器上。

{project_info}

重要：代码库目录下包含多个 git 仓库子目录。执行 git 命令时必须先 cd 到具体仓库子目录，否则 git 会作用于错误的仓库。

{base_context}{domain_prompt}
{context}{claude_md}
{knowledge_overview}"""

DEV_WORKFLOW = f"""\
## 工作流

你的工作遵循三个阶段，根据对话自然推进：

### 1. 探索（Explore）
- 回答用户关于项目代码、架构、模块的问题
- 主动阅读相关文档和代码，引用具体文件路径和代码位置
- 帮助用户理解技术背景、评估可行性
- 先从 CLAUDE.md、README.md、pom.xml（或 package.json）建立全貌，再按需深入
{FOCUS_RELATED}（CI/CD、部署、Docker 等无关领域）
{MIN_TOOL_CALLS}
{PARALLEL_CALLS}
{NO_REPEAT_READ}

用户可能只是在调研代码，不一定有开发意图。专注回答问题即可。\
如果用户表达了开发意图（如"需要改一下"/"帮我实现"/"能不能加个功能"），\
总结你理解的需求并询问用户确认。用户确认后，调用 `update_dev_phase` 进入方案设计。

### 2. 方案设计（Planning）
- 分析需求，阅读相关代码和文档，理解现有实现
- 不明确时主动提问，引用具体文件路径
- 输出结构化方案：影响范围、实现步骤、测试要点
- 方案完成后询问用户确认，用户确认后调用 `update_dev_phase` 进入编码实现

### 3. 编码实现（Implementing）
- **先创建 feature 分支**（格式：`YYYYMMDD-简短描述`，全小写英文短横线分隔）
- 按方案步骤修改代码
- 测试通过后 commit（**不要 push**），汇报修改内容和 commit message

## 阶段切换

使用 `update_dev_phase` 工具通知系统切换阶段。**必须在用户确认后才调用**，不要自行决定切换。

- 用户确认需求 → `update_dev_phase(phase="planning", summary="需求摘要")`
- 用户确认方案 → `update_dev_phase(phase="implementing", summary="方案摘要")`
- 需要回退重新设计 → `update_dev_phase(phase="planning", summary="回退原因")`

<HARD-GATE>
在用户确认需求和方案之前，不要修改任何代码文件。
探索和方案设计阶段只做读取和分析。
调用 update_dev_phase 前必须先获得用户的明确确认。
</HARD-GATE>

如果在编码过程中发现方案有重大问题（技术路线不可行、影响范围远超预期、\
关键依赖未考虑），主动告知用户并建议回退。用户同意后调用 \
`update_dev_phase(phase="planning")` 回到方案设计。
"""

DEV_TOOL_RULES = f"""\
工具使用规则（严格遵守）：
{NO_BASH_SEARCH}
{TOOL_MAPPING}
- 探索代码结构 → Task（子 agent 并行探索）
{BASH_ONLY_FOR}
{PARALLEL_CALLS}"""

DEV_DATA_QUERY_RULES = """\
线上数据查询规则：
- 当任务是查询线上数据库时，使用 MCP Playwright 工具操作数据库查询界面
- Playwright 是通过 MCP 工具调用的，**禁止用 Bash 执行 pip install playwright 或 playwright install**，直接使用 mcp__playwright__browser_navigate 等工具
- **不要去 GitLab 看代码**，数据查询不需要理解代码实现
- 如果需要理解业务概念，读本地知识库（biz/<domain>/knowledge/）即可
- 查询前先确定：目标集群名、目标库名、SQL 语句
- 安全红线：只允许 SELECT，禁止任何写操作（包括临时表），每批最多 5000 条（LIMIT 5000）
- 大数据量用分批查询 + 本地聚合（Map-Reduce 模式）"""

DEV_SAFETY_RULES = """\
安全规则：
- 禁止执行破坏性命令（rm -rf /、rm -rf ~、mkfs 等）
- 不要 git push，只做 branch + commit
- 仅在代码库 ({repos_path}) 范围内操作，不要读写其他路径
- 不确定的操作先问用户"""


def build_dev_prompt(ctx: dict) -> str:
    """Build the dev agent system prompt from context."""
    pp = ctx.get("repos_path", "")
    domains = ctx.get("domains", [])

    # Multi-domain: build path info for all domains
    if len(domains) > 1:
        r_paths = ctx.get("repos_paths", [])
        path_lines = []
        for i, d in enumerate(domains):
            rr = r_paths[i] if i < len(r_paths) else ""
            path_lines.append(f"- [{d}] 代码：{rr}")
        project_info = "项目信息：\n" + "\n".join(path_lines)
    else:
        project_info = f"项目信息：\n- 代码库根目录：{pp}"

    # Format optional context sections with headers
    base_ctx = ctx.get("base_context", "")
    base_ctx_block = f"公司背景：\n{base_ctx}\n\n" if base_ctx else ""
    context = ctx.get("context", "")
    context_block = f"业务上下文：\n{context}\n\n" if context else ""
    overview = ctx.get("knowledge_overview")
    overview_block = f"系统概览（参考）：\n{overview}\n" if overview else ""

    base = DEV_PROMPT_BASE.format(
        project_info=project_info,
        base_context=base_ctx_block,
        domain_prompt=ctx.get("domain_prompt", ""),
        context=context_block,
        claude_md=ctx.get("claude_md", ""),
        knowledge_overview=overview_block,
    )

    if len(domains) > 1:
        r_paths = ctx.get("repos_paths", [])
        all_paths = ", ".join(r_paths)
        safety = DEV_SAFETY_RULES.format(repos_path=all_paths)
    else:
        safety = DEV_SAFETY_RULES.format(repos_path=pp)

    # Worktree isolation hints
    worktree_hint = ""
    if ctx.get("worktree_active"):
        worktree_hint = "\n你的工作目录是独立副本（worktree），修改不会影响其他会话。\n"
    dirty_warning = ctx.get("dirty_warning", "")
    if dirty_warning:
        worktree_hint += f"\n⚠️ {dirty_warning}\n"

    tail = DEV_TOOL_RULES + "\n\n" + DEV_DATA_QUERY_RULES + "\n\n" + safety + worktree_hint + "\n\n用中文回复。"
    return base + ctx.get("roles_context", "") + DEV_WORKFLOW + tail


# ── Git helpers ──


def _subprocess_env() -> dict[str, str]:
    """Build an env dict with extended PATH for subprocess calls (homebrew, etc.)."""
    env = os.environ.copy()
    extra_paths = ["/usr/local/bin", "/opt/homebrew/bin"]
    current = env.get("PATH", "")
    for p in extra_paths:
        if p not in current:
            current = f"{current}:{p}"
    env["PATH"] = current
    return env


def _get_remote_url(path: str) -> str:
    """Get the origin remote URL for a git repo."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _parse_remote_url(remote_url: str) -> tuple[str, str, str]:
    """Parse a git remote URL into (host, namespace/project, platform)."""
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", remote_url)
    if ssh_match:
        host, proj_path = ssh_match.group(1), ssh_match.group(2)
        platform = "github" if "github" in host else "gitlab"
        return host, proj_path, platform

    https_match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", remote_url)
    if https_match:
        host, proj_path = https_match.group(1), https_match.group(2)
        platform = "github" if "github" in host else "gitlab"
        return host, proj_path, platform

    return "", "", "unknown"


def _build_mr_url(host: str, proj_path: str, branch: str, target: str = "") -> str:
    """Build a GitLab Merge Request creation URL."""
    if not target:
        target = "master"
    return (
        f"https://{host}/{proj_path}/-/merge_requests/new"
        f"?merge_request[source_branch]={branch}"
        f"&merge_request[target_branch]={target}"
    )


def find_feature_repos(domain: str, base_dir: str = "") -> list[dict]:
    """Find git repos on dev-agent-created feature branches (YYYYMMDD-* pattern).

    Args:
        domain: biz domain name
        base_dir: override repos directory (e.g. worktree path). Falls back to repos_path(domain).
    """
    repos_dir = Path(base_dir) if base_dir else Path(repos_path(domain))
    if not repos_dir.is_dir():
        return []
    results = []
    for d in repos_dir.iterdir():
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            continue
        if not branch or not re.match(r"^\d{8}-.+", branch):
            continue
        results.append({"path": str(d), "repo": d.name, "branch": branch})
    return results


def snapshot_repo_branches(
    domain: str, base_dir: str = ""
) -> dict[str, tuple[str, str]]:
    """Take a snapshot of current branches and commit hashes for all git repos.

    Args:
        domain: biz domain name
        base_dir: override repos directory (e.g. worktree path). Falls back to repos_path(domain).

    Returns:
        dict mapping repo name to (branch_name, commit_hash) tuple.
    """
    repos_dir = Path(base_dir) if base_dir else Path(repos_path(domain))
    if not repos_dir.is_dir():
        return {}
    result = {}
    for d in repos_dir.iterdir():
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            result[d.name] = (branch, commit)
        except Exception:
            result[d.name] = ("", "")
    return result


def find_changed_repos(
    domain: str, pre_branches: dict[str, tuple[str, str] | str], base_dir: str = ""
) -> list[dict]:
    """Compare current branches against a pre-snapshot to find repos where
    the branch or commit changed (i.e. agent created a new branch or committed).

    Args:
        domain: biz domain name
        pre_branches: snapshot from snapshot_repo_branches (supports both old str and new tuple format)
        base_dir: override repos directory (e.g. worktree path). Falls back to repos_path(domain).
    """
    repos_dir = Path(base_dir) if base_dir else Path(repos_path(domain))
    if not repos_dir.is_dir():
        return []
    results = []
    for d in repos_dir.iterdir():
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            continue
        if not branch or branch in ("main", "master"):
            continue
        old = pre_branches.get(d.name, "")
        if isinstance(old, tuple):
            old_branch, old_commit = old
        else:
            # Legacy format: str only
            old_branch, old_commit = old, ""
        if branch != old_branch or (old_commit and commit != old_commit):
            results.append({"path": str(d), "repo": d.name, "branch": branch})
    return results


def push_and_create_pr(repo_info: dict) -> dict:
    """Push branch and create PR/MR.

    - GitLab: uses git push options to create MR in a single command
    - GitHub: uses gh CLI
    - Fallback: push only, return browser URL or message
    """
    path = repo_info["path"]
    branch = repo_info["branch"]
    repo = repo_info["repo"]
    result = {"repo": repo, "branch": branch, "pr_url": "", "error": ""}
    env = _subprocess_env()

    remote_url = _get_remote_url(path)
    host, proj_path, platform = _parse_remote_url(remote_url)
    default_branch = detect_default_branch(path)

    if platform == "gitlab":
        _push_gitlab(result, path, branch, host, proj_path, default_branch, env)
    elif platform == "github":
        _push_and_gh_pr(result, path, branch, env)
    else:
        _push_plain(result, path, branch, env)

    return result


def _push_gitlab(
    result: dict,
    path: str,
    branch: str,
    host: str,
    proj_path: str,
    default_branch: str,
    env: dict,
) -> None:
    """Push + create GitLab MR via push options."""
    try:
        push = subprocess.run(
            [
                "git",
                "push",
                "-u",
                "origin",
                branch,
                "-o",
                "merge_request.create",
                "-o",
                f"merge_request.target={default_branch}",
                "-o",
                f"merge_request.title={branch}",
            ],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if push.returncode != 0:
            result["error"] = f"git push failed: {push.stderr.strip()}"
            return

        combined = (push.stdout + "\n" + push.stderr).strip()
        logger.info("git push output: %s", combined[:500])

        already_up_to_date = (
            "Everything up-to-date" in combined or "up-to-date" in combined
        )

        mr_url = ""
        for line in combined.splitlines():
            stripped = line.strip()
            if "http" in stripped and "merge_request" in stripped:
                for part in stripped.split():
                    if part.startswith("http"):
                        mr_url = part
                        break
                if mr_url:
                    break

        if mr_url and re.search(r"/merge_requests/\d+", mr_url):
            result["pr_url"] = mr_url
        else:
            if host and proj_path:
                result["pr_url"] = _build_mr_url(
                    host, proj_path, branch, default_branch
                )
            elif mr_url:
                result["pr_url"] = mr_url
            if already_up_to_date:
                result["error"] = "分支已在远程，请点击链接创建 MR"
            else:
                result["error"] = "已推送，MR 未自动创建，请点击链接手动创建"
    except Exception as e:
        result["error"] = f"git push error: {e}"


def _push_and_gh_pr(result: dict, path: str, branch: str, env: dict) -> None:
    """Push + create GitHub PR via gh CLI."""
    try:
        push = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if push.returncode != 0:
            result["error"] = f"git push failed: {push.stderr.strip()}"
            return
    except Exception as e:
        result["error"] = f"git push error: {e}"
        return

    gh = shutil.which("gh", path=env.get("PATH", ""))
    if not gh:
        result["error"] = "已推送，但 gh CLI 未安装，请手动创建 PR"
        return

    try:
        pr = subprocess.run(
            [gh, "pr", "create", "--title", branch, "--fill"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if pr.returncode != 0:
            if "already exists" in pr.stderr:
                view = subprocess.run(
                    [gh, "pr", "view", "--json", "url", "-q", ".url"],
                    cwd=path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
                if view.returncode == 0 and view.stdout.strip():
                    result["pr_url"] = view.stdout.strip()
            else:
                result["error"] = f"已推送，PR 创建失败: {pr.stderr.strip()}"
        else:
            result["pr_url"] = pr.stdout.strip()
    except Exception as e:
        result["error"] = f"已推送，PR 创建出错: {e}"


def _push_plain(result: dict, path: str, branch: str, env: dict) -> None:
    """Plain push for unknown platforms."""
    try:
        push = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if push.returncode != 0:
            result["error"] = f"git push failed: {push.stderr.strip()}"
        else:
            result["error"] = f"已推送到 origin/{branch}，请手动创建 PR/MR"
    except Exception as e:
        result["error"] = f"git push error: {e}"


# ── Register dev agent ──
# All phases use the same tools. HARD-GATE in prompt prevents premature writes.

DEV_AGENT = AgentConfig(
    name="dev",
    display_name="Dev",
    description="基于项目代码和文档，支持需求调研、方案设计、编码",
    command="/dev",
    tools=PROFILE_READWRITE,
    hooks={"PreToolUse": [BASH_HOOK]},
    requires_domain=True,
    include_repos=True,
    include_claude_md=True,
    max_turns=50,
    needs_isolation=True,
    max_budget_usd=3.00,
    build_prompt=build_dev_prompt,
)

register(DEV_AGENT)
