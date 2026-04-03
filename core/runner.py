"""Core agent execution loop — wraps Claude Agent SDK ClaudeSDKClient.

Architecture aligned with Claude Code's QueryEngine:
- Streaming response processing with per-turn PERF logging
- Cancel support via threading.Event
- Auto-retry on transient API errors (rate limit, overload)
- Token budget tracking with real-time logging
- Error classification with user-friendly messages
- Progress callbacks for streaming card updates

Uses ClaudeSDKClient (not query()) because in-process MCP servers and hooks
require bidirectional control protocol communication.
"""

import asyncio
import logging
import threading
import time
from typing import Callable

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
)

import core.audit as audit
from core.usage import RunResult

logger = logging.getLogger(__name__)

# ── Constants ──

MAX_RETRIES = 2          # retry transient API errors (rate limit, overload)
RETRY_DELAY_S = 3.0      # seconds between retries
RETRY_TOTAL_TIMEOUT_S = 15.0  # total time budget for all retries combined
TURN_TIMEOUT_S = 300     # per-turn timeout to detect SDK hangs

# ── Type aliases ──
# (text_so_far, status_line, is_done)
ProgressCallback = Callable[[str, str, bool], None]


# ── Tool name friendly display ──

_BUILTIN_TOOL_STATUS: dict[str, str] = {
    "Bash": "⚡ Bash",
    "Read": "📖 Read",
    "Write": "✏️ Write",
    "Edit": "✏️ Edit",
    "Glob": "🔍 Glob",
    "Grep": "🔍 Grep",
    "WebSearch": "🌐 WebSearch",
    "WebFetch": "🌐 WebFetch",
}


def _short_path(path: str) -> str:
    """Shorten a file path: keep last 2 segments."""
    if not path:
        return ""
    parts = path.rstrip("/").split("/")
    return path if len(parts) <= 3 else ".../" + "/".join(parts[-2:])


def _extract_tool_detail(tool_name: str, tool_input: dict) -> str:
    """Extract human-readable detail from tool input."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return (cmd[:60] + "...") if len(cmd) > 60 else cmd
    if tool_name == "Read":
        return _short_path(tool_input.get("file_path", ""))
    if tool_name in ("Write", "Edit"):
        return _short_path(tool_input.get("file_path", ""))
    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern}" + (f" in {_short_path(path)}" if path else "")
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f'"{pattern}"' + (f" in {_short_path(path)}" if path else "")
    if tool_name == "WebSearch":
        return tool_input.get("query", "")[:60]
    if tool_name == "WebFetch":
        return tool_input.get("url", "")[:60]
    if tool_name.startswith("mcp__"):
        for key in ("doc_id", "query", "search_key", "markdown", "title"):
            if key in tool_input:
                return f"{key}={str(tool_input[key])[:40]}"
    # SDK built-in tools: Agent/Task (subagent delegation), Skill, TodoWrite, etc.
    if tool_name in ("Agent", "Task"):
        sub_type = tool_input.get("subagent_type", tool_input.get("agent_type", ""))
        desc = tool_input.get("description", "")[:50]
        return f"type={sub_type} desc={desc}" if sub_type else desc
    if tool_name == "Skill":
        return tool_input.get("skill_name", tool_input.get("name", ""))[:40]
    # Fallback: show first key-value pair
    if tool_input:
        key = next(iter(tool_input))
        return f"{key}={str(tool_input[key])[:40]}"
    return ""


def _tool_status_line(tool_name: str, detail: str = "") -> str:
    """Convert a tool name + detail to a user-friendly status line."""
    base = _BUILTIN_TOOL_STATUS.get(tool_name, "")
    if not base:
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            action = parts[2] if len(parts) > 2 else tool_name
            base = f"📋 Feishu ({action})"
        else:
            base = f"🔧 {tool_name}"
    if detail:
        return f"{base} `{detail}`"
    return f"{base}..."


# ── Core agent loop ──


async def run_agent_core(
    text: str,
    options: ClaudeAgentOptions,
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
    chat_id: str = "",
    cancel_event: threading.Event | None = None,
    status_suffix: str = "",
) -> RunResult:
    """Shared agent loop. Returns a RunResult with text, session_id, cost, etc.

    status_suffix: optional text appended to the done status line
                   (e.g. "· Dev · my-app · 3h gap").
    """
    audit.log_request(sender_id, chat_id, text)

    result_text = ""
    session_id = None
    tool_count = 0
    turn_num = 0
    start_time = time.monotonic()
    turn_start = start_time
    total_cb_wait = 0.0
    loop = asyncio.get_running_loop()
    _last_future: asyncio.Future | None = None

    def _fire_progress(text_so_far: str, status: str, done: bool) -> None:
        nonlocal _last_future
        if on_progress:
            _last_future = loop.run_in_executor(
                None,
                on_progress,
                text_so_far,
                status,
                done,
            )

    async def _await_progress() -> None:
        nonlocal total_cb_wait
        if _last_future:
            cb_start = time.monotonic()
            await _last_future
            total_cb_wait += time.monotonic() - cb_start

    cancelled = False
    sdk_turns = 0
    duration_api = 0
    cost = 0.0
    stop_reason = ""
    input_tokens = 0
    output_tokens = 0
    usage: dict = {}
    _errored = False

    _pending_text: str | None = None  # buffered pure-text turn

    has_resume = bool(getattr(options, "resume", None))
    num_mcp = len(options.mcp_servers) if options.mcp_servers else 0
    _max_turns = getattr(options, "max_turns", None)
    _max_budget = getattr(options, "max_budget_usd", None)

    # Capture CLI subprocess stderr for debugging startup delays and crashes
    _cli_logger = logging.getLogger("core.runner.cli")

    def _on_cli_stderr(line: str) -> None:
        _cli_logger.info("[CLI] %s", line.rstrip())

    options.stderr = _on_cli_stderr

    logger.info(
        "[PERF] Start: chat=%s, resume=%s, mcp_servers=%d, max_turns=%s, budget=%s",
        chat_id,
        has_resume,
        num_mcp,
        _max_turns,
        f"${_max_budget:.2f}" if _max_budget else "none",
    )

    t_query_start = time.monotonic()
    first_message_logged = False

    # ── Retry loop for transient API errors ──
    retries = 0
    retry_start = time.monotonic()
    while True:
        client = ClaudeSDKClient(options=options)
        try:
            t_connect = time.monotonic()
            await client.connect()
            logger.info("[PERF] client.connect() in %.1fs", time.monotonic() - t_connect)

            await client.query(prompt=text)

            async for message in client.receive_response():
                # Check cancel signal at each message boundary
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    logger.info("Agent cancelled at turn %d for chat=%s", turn_num, chat_id)
                    try:
                        await client.interrupt()
                    except Exception:
                        pass
                    continue

                if not first_message_logged:
                    first_message_logged = True
                    startup = time.monotonic() - t_query_start
                    msg_type = type(message).__name__
                    logger.info(
                        "[PERF] First message after %.1fs (%s) — breakdown: connect + query + receive. chat=%s",
                        startup, msg_type, chat_id,
                    )

                if isinstance(message, AssistantMessage):
                    now = time.monotonic()
                    turn_num += 1
                    thinking_time = now - turn_start
                    turn_start = now

                    if _pending_text:
                        _fire_progress(_pending_text, "", False)
                        await _await_progress()
                        _pending_text = None

                    turn_text_parts: list[str] = []
                    tool_calls: list[tuple[str, dict]] = []
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            turn_text_parts.append(block.text)
                        if hasattr(block, "name"):
                            tool_input = getattr(block, "input", {}) or {}
                            tool_calls.append((block.name, tool_input))

                    turn_text = "\n".join(turn_text_parts).strip()

                    if tool_calls:
                        tool_count += len(tool_calls)
                        tool_names = [t[0] for t in tool_calls]
                        logger.info(
                            "[PERF] Turn %d: thinking=%.1fs, tools=%d (%s)",
                            turn_num, thinking_time, len(tool_calls), ", ".join(tool_names),
                        )
                        if turn_text:
                            _fire_progress(turn_text, "", False)
                            await _await_progress()

                        status_parts = []
                        for t_name, t_input in tool_calls:
                            detail = _extract_tool_detail(t_name, t_input)
                            logger.info("Tool call: %s (%s)", t_name, detail or "no detail")
                            logger.debug("Tool input: %s %s", t_name, t_input)
                            audit.log_tool_call(sender_id, chat_id, t_name, detail)
                            status_parts.append(_tool_status_line(t_name, detail))

                        _fire_progress("", " | ".join(status_parts), False)
                    elif turn_text:
                        _preview = (turn_text[:80] + "...") if len(turn_text) > 80 else turn_text
                        _preview = _preview.replace("\n", "\\n")
                        logger.info(
                            "[PERF] Turn %d: thinking=%.1fs, text-only (%d chars) %s",
                            turn_num, thinking_time, len(turn_text), _preview,
                        )
                        _pending_text = turn_text
                    else:
                        logger.info(
                            "[PERF] Turn %d: thinking=%.1fs, empty (no text/tools)",
                            turn_num, thinking_time,
                        )

                elif isinstance(message, ResultMessage):
                    session_id = message.session_id
                    sdk_turns = message.num_turns
                    cost = message.total_cost_usd or 0
                    duration_api = getattr(message, "duration_api_ms", 0)
                    usage = getattr(message, "usage", None) or {}
                    stop_reason = message.subtype or ""
                    input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
                    output_tokens = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
                    logger.info(
                        "Agent done: sdk_turns=%d, cost=$%.4f, subtype=%s, stop_reason=%s, "
                        "duration_api=%dms, usage=%s",
                        sdk_turns, cost, stop_reason,
                        getattr(message, "stop_reason", ""), duration_api, usage,
                    )
                    if message.result:
                        result_text = message.result

                else:
                    msg_type = type(message).__name__
                    logger.debug("SDK message: type=%s chat=%s", msg_type, chat_id)

        except Exception as exc:
            exc_str = str(exc)
            is_transient = any(k in exc_str.lower() for k in ("rate_limit", "429", "overload", "529", "timeout", "connection"))
            retry_elapsed = time.monotonic() - retry_start
            if is_transient and retries < MAX_RETRIES and retry_elapsed < RETRY_TOTAL_TIMEOUT_S:
                retries += 1
                logger.warning(
                    "[Retry] Transient error (attempt %d/%d): %s. Retrying in %.0fs...",
                    retries, MAX_RETRIES, exc_str[:100], RETRY_DELAY_S,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            logger.exception("SDK error in agent loop for chat=%s", chat_id)
            _errored = True
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Error during client disconnect", exc_info=True)
            break  # exit retry loop

    # Always fire done progress so the card never gets stuck.
    elapsed = time.monotonic() - start_time
    _sfx = f" {status_suffix}" if status_suffix else ""
    if _errored:
        done_status = f"⚠️ 出错 · {tool_count} tools · {int(elapsed)}s{_sfx}"
    elif cancelled:
        done_status = f"⏹ 已停止 · {tool_count} tools · {int(elapsed)}s{_sfx}"
    else:
        done_status = f"✓ {tool_count} tools · {int(elapsed)}s{_sfx}"
    _fire_progress("", done_status, True)
    try:
        await _await_progress()
    except Exception:
        pass

    # Summary perf log
    elapsed_api = duration_api / 1000.0 if duration_api else 0
    startup_time = (time.monotonic() - t_query_start) if not first_message_logged else 0
    # If first_message_logged, startup was already captured; recalculate for summary
    logger.info(
        "[PERF] Summary: turns=%d/%d, tools=%d, total=%.1fs, "
        "api=%.1fs, cb_wait=%.1fs, resume=%s, mcp=%d, "
        "cancelled=%s, errored=%s, chat=%s",
        turn_num,
        sdk_turns,
        tool_count,
        elapsed,
        elapsed_api,
        total_cb_wait,
        has_resume,
        num_mcp,
        cancelled,
        _errored,
        chat_id,
    )

    # Build RunResult with all collected metrics
    elapsed = time.monotonic() - start_time
    _result = RunResult(
        session_id=session_id,
        cost_usd=cost,
        sdk_turns=sdk_turns,
        tool_count=tool_count,
        duration_s=elapsed,
        stop_reason=(
            stop_reason
            if not cancelled and not _errored
            else ("cancelled" if cancelled else "error")
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage=usage if isinstance(usage, dict) and usage else None,
    )

    if cancelled:
        _result.text = "（已停止）"
        return _result

    if _errored:
        _result.text = (
            "⚠️ 处理过程中出现内部错误，请重试。如持续出错，使用 /clear 清除会话。"
        )
        return _result

    # Detect API errors returned as plain text by the SDK
    if result_text and result_text.startswith("API Error:"):
        logger.warning("API error in response: %s", result_text[:200])
        if (
            "budget_exceeded" in result_text
            or "Budget has been exceeded" in result_text
        ):
            _result.text = "⚠️ API 预算已用尽，请联系管理员增加额度。"
            return _result
        if "rate_limit" in result_text or "429" in result_text:
            _result.text = "⚠️ API 请求频率超限，请稍后重试。"
            return _result
        if "authentication" in result_text.lower() or "401" in result_text:
            _result.text = "⚠️ API 认证失败，请检查 API Key 配置。"
            return _result
        _result.text = "⚠️ API 调用出错，请稍后重试。如持续出错，联系管理员。"
        return _result

    # Budget exceeded: use pending text if the agent produced partial content
    if stop_reason == "error_max_budget_usd" and not result_text:
        if _pending_text:
            _result.text = _pending_text + "\n\n⚠️ 回复预算已用完，以上为部分回复。"
        else:
            _result.text = "⚠️ 回复预算已用完，未能生成回复。请简化问题或使用 /clear 清除会话后重试。"
        return _result

    # Max turns reached: use pending text if available
    if stop_reason == "max_turns" and not result_text:
        if _pending_text:
            _result.text = _pending_text + "\n\n⚠️ 回复轮次已用完，以上为部分回复。"
        else:
            _result.text = "⚠️ 回复轮次已用完，未能生成回复。请简化问题或使用 /clear 清除会话后重试。"
        return _result

    _result.text = result_text or "（处理完成，但没有文本回复）"
    return _result
