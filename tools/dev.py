"""Dev tools: write access + bash execution.

Safety hooks inspired by Claude Code's permission system:
- Bash guard: blocks exploration commands that should use dedicated tools
- Git guard: blocks destructive git operations
- Destructive command guard: blocks rm -rf, mkfs, dd, etc.
"""

import logging
import shlex

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)

TOOLS = ["Write", "Edit", "Bash"]

# ── Bash guard hook ──
# Block Bash commands that should use dedicated tools (Glob/Grep/Read).

_BASH_BLOCKED_CMDS = frozenset({
    "ls", "find", "grep", "rg", "cat", "head", "tail",
    "wc", "tree", "file", "stat", "du",
})

_BASH_REDIRECT = {
    "ls":   "Use Glob (e.g. `src/*/`) to list directories.",
    "find": "Use Glob (e.g. `**/*.java`) to find files.",
    "grep": "Use Grep tool to search file contents.",
    "rg":   "Use Grep tool to search file contents.",
    "cat":  "Use Read tool to read files.",
    "head": "Use Read tool with offset/limit to read files.",
    "tail": "Use Read tool with offset/limit to read files.",
    "wc":   "Use Read tool or Glob tool instead.",
    "tree": "Use Glob (e.g. `**/*`) to explore directory structure.",
    "file": "Use Read tool to inspect files.",
    "stat": "Use Glob or Read tool instead.",
    "du":   "Use Glob tool instead.",
}

# ── Destructive command guard ──
# Block commands that can cause irreversible damage.

_DESTRUCTIVE_PATTERNS = [
    ("rm", ["-rf /", "-rf ~", "-rf /*"]),
    ("mkfs", []),
    ("dd", ["if=/dev/zero", "if=/dev/random"]),
    ("chmod", ["-R 777 /", "-R 000 /"]),
    ("chown", ["-R"]),
]


def _check_destructive(command: str) -> str | None:
    """Check for destructive system commands. Returns denial reason or None."""
    cmd_lower = command.lower().strip()

    # Block package installation — agent should not modify its own runtime environment
    _INSTALL_PATTERNS = [
        "pip install", "pip3 install", ".venv/bin/pip install",
        "npm install", "yarn add", "brew install",
        "playwright install",
    ]
    for pattern in _INSTALL_PATTERNS:
        if pattern in cmd_lower:
            return f"禁止执行 {pattern}，不要修改运行环境。如需浏览器操作请使用 mcp__playwright__* MCP 工具。"

    # Direct pattern matches
    for cmd, patterns in _DESTRUCTIVE_PATTERNS:
        if not cmd_lower.startswith(cmd):
            continue
        if not patterns:
            return f"禁止执行 {cmd} 命令，该操作可能导致不可逆的系统损坏。"
        for pattern in patterns:
            if pattern in cmd_lower:
                return f"禁止执行 {cmd} {pattern}，该操作会导致数据丢失。"

    # Fork bomb
    if ":()" in cmd_lower and ":|:" in cmd_lower:
        return "禁止执行 fork bomb，该操作会耗尽系统资源。"

    # Pipe to shell from curl/wget (code injection risk)
    if ("curl " in cmd_lower or "wget " in cmd_lower) and ("| sh" in cmd_lower or "| bash" in cmd_lower):
        return "禁止从网络下载并直接执行脚本，请先下载审查后再执行。"

    return None


# ── Git safety guard ──

def _check_git_safety(command: str) -> str | None:
    """Check a git command for destructive operations."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    git_idx = None
    for i, t in enumerate(tokens):
        if t == "git" or t.endswith("/git"):
            git_idx = i
            break
    if git_idx is None:
        return None

    rest = tokens[git_idx + 1:]
    if not rest:
        return None

    subcmd = rest[0]
    args = rest[1:]

    if subcmd == "push":
        return "禁止直接 git push，请使用 /dev push 命令推送。"

    if subcmd == "reset" and "--hard" in args:
        return "禁止 git reset --hard，该操作会丢失未提交的修改。"

    if subcmd == "clean":
        for a in args:
            if a.startswith("-") and "f" in a:
                return "禁止 git clean -f，该操作会删除未跟踪的文件。"

    if subcmd == "checkout":
        if "-b" in args or "-B" in args:
            return None
        non_flag = [a for a in args if not a.startswith("-")]
        if "." in non_flag:
            return "禁止 git checkout .，该操作会丢弃所有未提交的修改。"

    if subcmd == "branch" and "-D" in args:
        return "禁止 git branch -D（强制删除），请使用 -d（安全删除）。"

    if subcmd == "rebase" and "--force" not in args:
        # Allow normal rebase, block force push after rebase
        pass

    return None


async def _bash_guard_hook(hook_input, tool_use_id, context):
    """PreToolUse hook: block exploration commands, destructive ops, and unsafe git."""
    command = hook_input.get("tool_input", {}).get("command", "").strip()

    # 1. Check destructive commands first (highest priority)
    reason = _check_destructive(command)
    if reason:
        logger.info("[HOOK] Blocked destructive command: %s → %s", command[:80], reason)
        return {
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }

    # 2. Extract first word for tool redirect check
    first_word = ""
    for token in command.split():
        if "=" in token and not token.startswith("-"):
            continue
        first_word = token.split("/")[-1]
        break

    if first_word in _BASH_BLOCKED_CMDS:
        hint = _BASH_REDIRECT.get(first_word, "Use dedicated tools instead of Bash.")
        logger.info("[HOOK] Blocked Bash command: %s → %s", first_word, hint)
        return {
            "reason": hint,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": hint,
            },
        }

    # 3. Git safety check
    if first_word == "git":
        reason = _check_git_safety(command)
        if reason:
            logger.info("[HOOK] Blocked git command: %s → %s", command[:80], reason)
            return {
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }

    return {}


BASH_HOOK = HookMatcher(matcher="Bash", hooks=[_bash_guard_hook])
