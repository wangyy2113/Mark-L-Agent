"""Path-based access control for tool operations.

Validates file paths against allowed patterns (e.g. biz/*/repos)
before tool execution. Implemented as a Claude Agent SDK pre-tool-use hook.
"""

import fnmatch
import logging
from pathlib import Path

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)


def check_path(file_path: str, allowed_patterns: list[str], base_dir: str = "") -> bool:
    """Check if a file path is within allowed patterns.

    Patterns use fnmatch glob syntax relative to base_dir's parent.
    Example: pattern "biz/*/repos" matches any file under biz/<domain>/repos/.

    Args:
        file_path: Path to check (absolute or relative).
        allowed_patterns: Glob patterns like ["biz/*/repos", "biz/*/docs"].
        base_dir: Base path for resolving patterns (typically BIZ_BASE_PATH parent).

    Returns:
        True if path is allowed (or no restrictions), False otherwise.
    """
    if not allowed_patterns:
        return True  # No restriction

    p = Path(file_path).resolve()

    for pattern in allowed_patterns:
        if base_dir:
            abs_pattern = str(Path(base_dir).resolve() / pattern)
        else:
            abs_pattern = str(Path(pattern).resolve())

        # Split both into parts and match prefix
        file_parts = p.parts
        pattern_parts = Path(abs_pattern).parts

        if len(file_parts) >= len(pattern_parts):
            # File is deeper than or equal to pattern: match pattern as prefix
            prefix = file_parts[:len(pattern_parts)]
            if all(fnmatch.fnmatch(fp, pp) for fp, pp in zip(prefix, pattern_parts)):
                return True
        else:
            # File is shallower than pattern: allow if it's a parent of allowed area
            # (e.g. Grep on /biz when pattern is /biz/*/repos)
            prefix = pattern_parts[:len(file_parts)]
            if all(fnmatch.fnmatch(fp, pp) for fp, pp in zip(file_parts, prefix)):
                return True

    return False


# Tool name → parameter that contains the file path
_PATH_PARAMS: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "path",
    "Grep": "path",
}


def make_path_guard_hook(
    allowed_paths: list[str],
    base_dir: str = "",
) -> HookMatcher | None:
    """Create a pre-tool-use hook that enforces path restrictions.

    Returns None if no restrictions (empty allowed_paths).
    """
    if not allowed_paths:
        return None

    async def _path_guard(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})

        param_name = _PATH_PARAMS.get(tool_name)
        if not param_name:
            return {}  # Not a path-based tool

        path_value = tool_input.get(param_name, "")
        if not path_value:
            return {}  # No path specified (e.g. Glob in cwd)

        if check_path(path_value, allowed_paths, base_dir):
            return {}

        msg = f"路径访问受限：{path_value} 不在允许范围内。"
        logger.info("[PATH_GUARD] Denied %s on %s", tool_name, path_value)
        return {
            "reason": msg,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": msg,
            },
        }

    return HookMatcher(
        matcher="Read|Write|Edit|Glob|Grep",
        hooks=[_path_guard],
    )
