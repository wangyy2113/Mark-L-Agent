"""Built-in tools for the lite agent provider.

Provides local tool implementations and OpenAI function-calling schemas.
These replace the Claude Agent SDK's built-in tools (Read, Write, Edit,
Bash, Glob, Grep) when running in lite mode.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT = 120  # seconds
MAX_READ_LINES = 2000
MAX_OUTPUT_SIZE = 50_000  # chars


# ── Tool implementations ─────────────────────────────────────


def bash(command: str, timeout: int = COMMAND_TIMEOUT, cwd: str = "") -> str:
    """Execute a shell command."""
    work_dir = cwd or str(Path.home())
    work_dir = str(Path(work_dir).expanduser())
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        if len(output) > MAX_OUTPUT_SIZE:
            output = (
                output[:MAX_OUTPUT_SIZE]
                + f"\n... (truncated, total {len(result.stdout) + len(result.stderr)} chars)"
            )
        return json.dumps(
            {"exit_code": result.returncode, "output": output},
            ensure_ascii=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def read_file(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    """Read file contents with line numbers, or list a directory."""
    p = Path(path).expanduser()
    try:
        if p.is_dir():
            entries = sorted(p.iterdir())
            lines = []
            for e in entries:
                suffix = "/" if e.is_dir() else ""
                lines.append(f"{e.name}{suffix}")
            return "\n".join(lines) or "(empty directory)"

        text = p.read_text(errors="replace")
        all_lines = text.splitlines(keepends=True)
        start = max(0, offset - 1)  # offset is 1-indexed
        selected = all_lines[start : start + limit]
        numbered = [f"{start + i + 1}: {line}" for i, line in enumerate(selected)]
        result = "".join(numbered)
        if len(result) > MAX_OUTPUT_SIZE:
            result = result[:MAX_OUTPUT_SIZE] + "\n... (truncated)"
        return result
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {path}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed."""
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return json.dumps({"error": str(e)})


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace a string in a file (first occurrence only).

    Fails if 0 or >1 matches are found, to prevent ambiguous edits.
    """
    p = Path(path).expanduser()
    try:
        content = p.read_text()
        if old_string not in content:
            return json.dumps({"error": "oldString not found in file"})
        count = content.count(old_string)
        if count > 1:
            return json.dumps(
                {
                    "error": f"Found {count} matches for oldString. "
                    "Provide more surrounding context to identify the correct match."
                }
            )
        new_content = content.replace(old_string, new_string, 1)
        p.write_text(new_content)
        return "Edit applied successfully."
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def glob_files(pattern: str, path: str = ".") -> str:
    """Search for files matching a glob pattern."""
    p = Path(path).expanduser()
    try:
        matches = sorted(p.glob(pattern))[:200]  # cap results
        return "\n".join(str(m) for m in matches) or "(no matches)"
    except Exception as e:
        return json.dumps({"error": str(e)})


def grep_content(pattern: str, path: str = ".", include: str = "") -> str:
    """Search file contents using ripgrep (rg)."""
    cmd = ["rg", "--line-number", "-e", pattern]
    if include:
        cmd.extend(["-g", include])
    cmd.append(str(Path(path).expanduser()))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + "\n... (truncated)"
        return output or "(no matches)"
    except FileNotFoundError:
        return json.dumps(
            {"error": "ripgrep (rg) not found. Install with: brew install ripgrep"}
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Search timed out after 30s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Dispatch table ────────────────────────────────────────────

_DISPATCH = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "glob_files": glob_files,
    "grep_content": grep_content,
}


def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name. Returns result as string."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return fn(**args)
    except TypeError as e:
        # Bad arguments from the model
        logger.warning("Tool %s bad args: %s", name, e)
        return json.dumps({"error": f"Invalid arguments for {name}: {e}"})
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return json.dumps({"error": str(e)})


# ── OpenAI function-calling schemas ───────────────────────────

TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "在本地机器执行 shell 命令。支持管道、重定向等。"
                "默认超时 120 秒。用于运行 git、python、ls、grep、curl 等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数（默认 120）",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "工作目录（可选）",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取文件内容（带行号）或列出目录。"
                "大文件用 offset/limit 分页读取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "起始行号（1-indexed，默认 1）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多读取行数（默认 2000）",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "将内容写入文件。自动创建父目录。已有文件会被覆盖。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件的绝对路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的内容",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "替换文件中的字符串。找到 old_string 的唯一匹配并替换为 new_string。"
                "如果找到 0 个或多于 1 个匹配则失败。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件的绝对路径",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要查找替换的精确字符串",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的字符串",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "按 glob 模式搜索文件（如 '**/*.py'）。最多返回 200 个结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 模式",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录（默认当前目录）",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_content",
            "description": (
                "用正则表达式搜索文件内容（基于 ripgrep）。"
                "返回匹配行及行号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录（默认当前目录）",
                    },
                    "include": {
                        "type": "string",
                        "description": "文件过滤，如 '*.py'（可选）",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]
