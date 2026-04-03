"""Lightweight agent loop using OpenAI-compatible Chat Completion API.

This module provides run() and run_dev() with the same signatures as
agent.py, so agent.py can route to it when LLM_PROVIDER=lite.

Uses the `openai` package to call any OpenAI-compatible endpoint
(Gemini, Groq, DeepSeek, SiliconFlow, etc.) with function calling.
"""

import json
import logging
import threading
import time
from typing import Callable

import core.audit as audit
from core.config import get_settings
from providers.builtin_tools import TOOLS_SCHEMA, execute_tool

logger = logging.getLogger(__name__)

# ── Type alias (same as agent.py) ──
# (text_so_far, status_line, is_done)
ProgressCallback = Callable[[str, str, bool], None]

# ── Constants ──
MAX_ROUNDS = 15
MAX_RETRIES = 5
RETRY_DELAYS = [1, 2, 4, 8, 15]  # seconds

# ── Per-chat concurrency locks ──
_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_meta = threading.Lock()

# ── Tool status display ──
_TOOL_STATUS: dict[str, str] = {
    "bash": "⚡ Bash",
    "read_file": "📖 Read",
    "write_file": "✏️ Write",
    "edit_file": "✏️ Edit",
    "glob_files": "🔍 Glob",
    "grep_content": "🔍 Grep",
}

# ── Tool detail key mapping ──
_TOOL_DETAIL_KEY: dict[str, str] = {
    "bash": "command",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob_files": "pattern",
    "grep_content": "pattern",
}


def _get_lock(chat_id: str) -> threading.Lock:
    with _chat_locks_meta:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


def _get_client():
    """Create an OpenAI client. Imported lazily to avoid hard dependency."""
    from openai import OpenAI

    s = get_settings()
    kwargs = {"api_key": s.lite_api_key}
    if s.lite_base_url:
        kwargs["base_url"] = s.lite_base_url
    return OpenAI(**kwargs)


def _extract_lite_tool_detail(tool_name: str, tool_args: dict) -> str:
    """Extract human-readable detail from lite tool args."""
    key = _TOOL_DETAIL_KEY.get(tool_name, "")
    if not key:
        return ""
    val = str(tool_args.get(key, ""))
    if not val:
        return ""
    if len(val) > 60:
        val = val[:60] + "..."
    return val


def _tool_status(name: str, detail: str = "") -> str:
    base = _TOOL_STATUS.get(name, name)
    if detail:
        return f"{base} `{detail}`"
    return f"{base}..."


def _chat_loop(
    messages: list[dict],
    model: str,
    tools: list[dict],
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
    chat_id: str = "",
) -> str:
    """Run the ReAct loop. Returns final text response."""
    client = _get_client()

    for _round in range(MAX_ROUNDS):
        logger.info(
            "Lite agent round %d, messages=%d, tools=%d",
            _round + 1,
            len(messages),
            len(tools),
        )

        # Call with retry on rate limits
        response = _call_with_retry(client, model, messages, tools)

        choice = response.choices[0]
        assistant_msg = choice.message

        # Log token usage
        if response.usage:
            logger.info(
                "Token usage: input=%d, output=%d, total=%d",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens,
            )

        # Append assistant message to conversation
        messages.append(assistant_msg.model_dump(exclude_none=True))

        # If no tool calls, return the final text
        # Don't fire progress — caller (event_handler) handles the final card update
        if not assistant_msg.tool_calls:
            text = assistant_msg.content or ""
            return text or "（处理完成，但没有文本回复）"

        # Intermediate text accompanies tool calls → send as independent card
        if assistant_msg.content and on_progress:
            on_progress(assistant_msg.content, "", False)

        # Execute each tool call and build combined status
        status_parts: list[str] = []
        for tool_call in assistant_msg.tool_calls:
            fn = tool_call.function
            tool_name = fn.name
            try:
                tool_args = json.loads(fn.arguments) if fn.arguments else {}
            except json.JSONDecodeError:
                logger.warning("Bad JSON in tool args for %s: %s", tool_name, fn.arguments)
                tool_args = {}

            detail = _extract_lite_tool_detail(tool_name, tool_args)
            logger.info("Tool call: %s (%s)", tool_name, detail or "no detail")
            audit.log_tool_call(sender_id, chat_id, tool_name, detail)
            status_parts.append(_tool_status(tool_name, detail))

            result_str = execute_tool(tool_name, tool_args)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str,
                }
            )

        # Notify combined tool status
        if on_progress and status_parts:
            on_progress("", " | ".join(status_parts), False)

    logger.warning("Lite agent hit max rounds (%d) for chat %s", MAX_ROUNDS, chat_id)
    return "（达到最大工具调用轮数，请简化请求）"


def _call_with_retry(client, model: str, messages: list[dict], tools: list[dict]):
    """Call chat completions with stepped backoff on rate limits."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            return client.chat.completions.create(**kwargs)
        except Exception as e:
            # Check if it's a rate limit error (status 429)
            is_rate_limit = (
                hasattr(e, "status_code") and e.status_code == 429
            ) or "rate" in str(e).lower()

            if is_rate_limit and attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "Rate limited, retry %d/%d after %ds",
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
            else:
                raise


# ── Public entry points ───────────────────────────────────────
# Same signatures as agent.run() / agent.run_dev()


def run(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    is_admin: bool = True,
    sender_id: str = "",
) -> str:
    """Synchronous entry point for normal chat (lite mode)."""
    lock = _get_lock(chat_id)
    if not lock.acquire(timeout=120):
        raise TimeoutError("该会话正忙，请稍后再试")
    try:
        audit.log_request(sender_id, chat_id, text)
        s = get_settings()

        # Import system prompt builder from agent module
        from agent import build_system_prompt

        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": text},
        ]

        result = _chat_loop(
            messages=messages,
            model=s.lite_model,
            tools=TOOLS_SCHEMA,
            on_progress=on_progress,
            sender_id=sender_id,
            chat_id=chat_id,
        )

        return result
    finally:
        lock.release()


def run_dev(
    chat_id: str,
    text: str,
    on_progress: ProgressCallback | None = None,
    sender_id: str = "",
) -> str:
    """Synchronous entry point for dev mode (lite mode)."""
    lock = _get_lock(chat_id)
    if not lock.acquire(timeout=300):
        raise TimeoutError("该会话正忙，请稍后再试")
    try:
        audit.log_request(sender_id, chat_id, text)
        s = get_settings()

        # Build dev system prompt with domain context
        from agent import (
            DEV_SYSTEM_PROMPT,
            _agent_session_store,
        )
        from core.biz import (
            repos_path as _biz_repos_path,
            load_domain_prompt as _load_domain_prompt,
        )

        dev_state = _agent_session_store.get(chat_id) if _agent_session_store else None
        domain = dev_state.domain if dev_state else ""
        requirement = dev_state.requirement if dev_state else ""

        system_prompt = DEV_SYSTEM_PROMPT.format(
            repos_path=_biz_repos_path(domain),
            domain_prompt=_load_domain_prompt(domain),
            requirement=requirement,
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        result = _chat_loop(
            messages=messages,
            model=s.lite_model,
            tools=TOOLS_SCHEMA,
            on_progress=on_progress,
            sender_id=sender_id,
            chat_id=chat_id,
        )

        return result
    finally:
        lock.release()
