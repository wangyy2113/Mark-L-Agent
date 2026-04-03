"""Built-in scheduled task scheduler.

Runs time-based tasks in a daemon thread. Uses Python stdlib only (no extra deps).
Each task fetches Feishu group messages via lark-cli, analyzes them with the LLM,
and sends a summary back to the target chat.

Design decisions:
- Pure stdlib: threading.Timer-based tick loop, datetime for schedule matching.
- Each task runs in its own daemon thread to avoid blocking the tick loop.
- LLM calls use anthropic SDK directly (single completion, no agent session).
- lark-cli subprocess calls have timeouts and graceful error handling.
- On LLM failure, sends a plain-text stats fallback message instead.
"""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Task definitions ──────────────────────────────────────────────────────────

SCHEDULED_TASKS: list[dict[str, Any]] = [
    # Example scheduled task:
    # {
    #     "name": "Alert Summary",
    #     "source_chat_id": "oc_xxxx",       # Feishu group to read messages from
    #     "target_chat_id": "oc_yyyy",       # Feishu group to send summary to
    #     "schedule_hours": [11, 16],         # Run at 11:00 and 16:00
    #     "lookback_hours": 5,                # Analyze last 5 hours of messages
    #     "page_size": 50,
    #     "system_prompt": "Analyze alert messages and generate a summary report.",
    #     "empty_message": "No alerts in the last 5 hours.",
    # },
]

# ── lark-cli helpers ──────────────────────────────────────────────────────────

def _build_env() -> dict[str, str]:
    """Build environment dict for subprocess calls, inheriting PATH."""
    env = os.environ.copy()
    # Ensure common binary paths are included (macOS/Linux)
    extra_paths = ["/usr/local/bin", "/usr/bin", "/opt/homebrew/bin", os.path.expanduser("~/.local/bin")]
    existing = env.get("PATH", "")
    added = ":".join(p for p in extra_paths if p not in existing)
    if added:
        env["PATH"] = added + ":" + existing
    return env


def _fetch_messages(chat_id: str, lookback_hours: int, page_size: int) -> list[dict] | None:
    """Fetch recent messages from a Feishu chat via lark-cli.

    Returns a list of message dicts, or None on failure (to distinguish from empty).
    """
    start_iso = (datetime.now() - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    cmd = [
        "lark-cli", "im", "+chat-messages-list",
        "--chat-id", chat_id,
        "--start", start_iso,
        "--page-size", str(page_size),
        "--sort", "asc",
    ]
    logger.info("Fetching messages: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=_build_env(),
        )
        if result.returncode != 0:
            logger.error(
                "lark-cli fetch failed (code=%d): stderr=%s",
                result.returncode,
                result.stderr[:500],
            )
            return None
        # lark-cli outputs JSON
        raw = result.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        # lark-cli wraps response in {"ok": true, "data": {"messages": [...]}}
        if isinstance(data, dict):
            inner = data.get("data", data)  # unwrap outer envelope
            if isinstance(inner, dict):
                return inner.get("messages", inner.get("items", []))
            return []
        if isinstance(data, list):
            return data
        return []
    except subprocess.TimeoutExpired:
        logger.error("lark-cli fetch timed out for chat_id=%s", chat_id)
        return None
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse lark-cli output: %s", exc)
        return None
    except FileNotFoundError:
        logger.error("lark-cli not found in PATH. Please install it.")
        return None
    except Exception:
        logger.exception("Unexpected error fetching messages for chat_id=%s", chat_id)
        return None


def _send_message(chat_id: str, text: str) -> bool:
    """Send a text message to a Feishu chat via lark-cli.

    Returns True on success.
    """
    cmd = [
        "lark-cli", "im", "+messages-send",
        "--chat-id", chat_id,
        "--text", text,
        "--as", "bot",
    ]
    logger.info("Sending message to chat_id=%s (%d chars)", chat_id, len(text))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            env=_build_env(),
        )
        if result.returncode != 0:
            logger.error(
                "lark-cli send failed (code=%d): stderr=%s",
                result.returncode,
                result.stderr[:500],
            )
            return False
        logger.info("Message sent to chat_id=%s", chat_id)
        return True
    except subprocess.TimeoutExpired:
        logger.error("lark-cli send timed out for chat_id=%s", chat_id)
        return False
    except FileNotFoundError:
        logger.error("lark-cli not found in PATH.")
        return False
    except Exception:
        logger.exception("Unexpected error sending message to chat_id=%s", chat_id)
        return False


# ── LLM analysis ─────────────────────────────────────────────────────────────

def _extract_card_text(obj: dict) -> str:
    """Deep-extract text from Feishu interactive card / rich text JSON.

    SLS alert cards have structured content with nested elements.
    This recursively extracts all text content, including the critical
    'msg' field that contains error messages and stack traces.
    """
    texts: list[str] = []

    def _walk(node):
        if isinstance(node, str):
            stripped = node.strip()
            if stripped:
                texts.append(stripped)
        elif isinstance(node, dict):
            # Direct text fields (common in cards)
            for key in ("text", "content", "msg", "title", "value"):
                val = node.get(key)
                if isinstance(val, str) and val.strip():
                    texts.append(val.strip())
                elif isinstance(val, (list, dict)):
                    _walk(val)
            # Card elements array
            for key in ("elements", "fields", "columns", "rows", "actions"):
                val = node.get(key)
                if isinstance(val, list):
                    _walk(val)
            # Tag-based rich text elements
            if node.get("tag") == "text" and "text" in node:
                pass  # already handled above
            elif node.get("tag") in ("div", "column", "markdown"):
                _walk(node.get("text", ""))
                _walk(node.get("content", ""))
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return "\n".join(texts) if texts else ""


def _messages_to_text(messages: list[dict]) -> str:
    """Convert a list of message dicts to a plain-text block for the LLM."""
    lines: list[str] = []
    for i, msg in enumerate(messages, 1):
        create_time = msg.get("create_time", "")
        msg_type = msg.get("msg_type", "")

        # Extract content — lark-cli puts it directly in "content" field
        content_str = msg.get("content", "")
        if not content_str:
            body = msg.get("body", {})
            if isinstance(body, dict):
                content_str = body.get("content", "")
            elif isinstance(body, str):
                content_str = body

        # Parse JSON-encoded content (rich text / interactive cards)
        if content_str and content_str.lstrip()[:1] in ("{", "[", "<"):
            if content_str.lstrip().startswith("<card"):
                pass  # Already readable
            else:
                try:
                    content_obj = json.loads(content_str)
                    extracted = _extract_card_text(content_obj)
                    if extracted:
                        content_str = extracted
                except (json.JSONDecodeError, TypeError):
                    pass

        if content_str:
            ts_str = f" [{create_time}]" if create_time else ""
            lines.append(f"{i}.{ts_str} {content_str[:800]}")

    return "\n".join(lines) if lines else ""


def _analyze_with_llm(messages: list[dict], system_prompt: str, task_name: str) -> str | None:
    """Call the LLM to analyze messages and return a summary.

    Uses OpenAI-compatible API (works with Anthropic via base_url proxy).
    Returns the summary string, or None on failure.
    """
    try:
        import openai
        from core.config import get_settings
        s = get_settings()
    except ImportError:
        logger.error("openai package not available for scheduler LLM calls")
        return None
    except Exception:
        logger.exception("Failed to get settings for LLM call")
        return None

    messages_text = _messages_to_text(messages)
    if not messages_text:
        logger.info("[%s] No parseable message content, skipping LLM call", task_name)
        return None

    user_content = (
        f"以下是最近5小时内的消息记录（共 {len(messages)} 条）：\n\n"
        f"{messages_text}\n\n"
        "请按照系统提示的要求生成摘要报告。"
    )

    try:
        # Use anthropic_base_url + anthropic_api_key via OpenAI-compatible endpoint
        base_url = s.anthropic_base_url.rstrip("/") if s.anthropic_base_url else "https://api.anthropic.com"
        # Ensure it ends with /v1 for OpenAI-compatible format
        if not base_url.endswith("/v1"):
            base_url = base_url + "/v1"

        client = openai.OpenAI(
            api_key=s.anthropic_api_key,
            base_url=base_url,
        )
        # Use the configured model
        model = s.claude_model
        response = client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
        logger.info("[%s] LLM analysis done (%d chars, model=%s)", task_name, len(text or ""), model)
        return (text or "").strip() or None
    except Exception:
        logger.exception("[%s] LLM call failed", task_name)
        return None


# ── Task runner ───────────────────────────────────────────────────────────────

def _run_task(task: dict[str, Any]) -> None:
    """Execute a single scheduled task in the current thread.

    Flow:
    1. Fetch messages from source chat.
    2. Analyze with LLM.
    3. Send summary (or fallback) to target chat.
    """
    name = task["name"]
    source_chat_id = task["source_chat_id"]
    target_chat_id = task["target_chat_id"]
    lookback_hours = task.get("lookback_hours", 5)
    page_size = task.get("page_size", 50)
    system_prompt = task["system_prompt"]
    empty_message = task.get("empty_message", "最近无消息")

    logger.info("[Scheduler] Starting task: %s", name)
    start = time.monotonic()

    # Step 1: fetch messages
    messages = _fetch_messages(source_chat_id, lookback_hours, page_size)
    if messages is None:
        logger.error("[Scheduler][%s] Fetch failed, skipping this run", name)
        return
    logger.info("[Scheduler][%s] Fetched %d messages", name, len(messages))

    # Step 2: determine summary
    if not messages:
        # Genuinely no messages — skip silently, don't send "no alerts" noise
        logger.info("[Scheduler][%s] No messages in the last %dh, skipping", name, lookback_hours)
        return
    else:
        llm_result = _analyze_with_llm(messages, system_prompt, name)
        if llm_result:
            summary = llm_result
        else:
            # Fallback: plain stats when LLM fails
            summary = (
                f"【{name}】统计（最近{lookback_hours}小时）\n"
                f"共收到 {len(messages)} 条消息，LLM 分析失败，请手动查看告警群。"
            )
            logger.warning("[Scheduler][%s] LLM failed, sending fallback stats", name)

    # Add task header for clarity in the target chat
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    final_text = f"【{name}】{now_str}\n{summary}"

    # Step 3: send to target chat
    ok = _send_message(target_chat_id, final_text)
    elapsed = time.monotonic() - start
    if ok:
        logger.info("[Scheduler][%s] Done in %.1fs", name, elapsed)
    else:
        logger.error("[Scheduler][%s] Failed to send summary after %.1fs", name, elapsed)


def _run_task_safe(task: dict[str, Any]) -> None:
    """Thread-safe wrapper for _run_task with full exception guard."""
    name = task.get("name", "unknown")
    try:
        _run_task(task)
    except Exception:
        logger.exception("[Scheduler] Unhandled error in task: %s", name)


# ── Scheduler loop ────────────────────────────────────────────────────────────

class Scheduler:
    """Lightweight cron-like scheduler backed by a daemon thread.

    Wakes up every minute, checks if any task is due, and fires it in a
    separate daemon thread so tasks don't block each other or the tick loop.
    """

    def __init__(self, tasks: list[dict[str, Any]], stop_event: threading.Event | None = None) -> None:
        self._tasks = tasks
        self._stop_event = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        # Track last execution: {task_name: (date, hour)} to prevent double-fire within same hour
        self._last_run: dict[str, tuple[Any, int]] = {}

    def start(self) -> None:
        """Start the scheduler in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("[Scheduler] Already running")
            return
        # Mark current hour as already ran to prevent immediate fire on restart
        now = datetime.now()
        for task in self._tasks:
            schedule_hours = task.get("schedule_hours", [])
            if now.hour in schedule_hours:
                self._mark_ran(task, now)
                logger.info(
                    "[Scheduler] Skipping '%s' for hour %d (startup grace)",
                    task["name"], now.hour,
                )
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="scheduler",
        )
        self._thread.start()
        logger.info(
            "[Scheduler] Started with %d task(s): %s",
            len(self._tasks),
            [t["name"] for t in self._tasks],
        )

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()

    def _should_run(self, task: dict[str, Any], now: datetime) -> bool:
        """Check if a task is due at the current time (hour-level granularity)."""
        schedule_hours = task.get("schedule_hours", [])
        if now.hour not in schedule_hours:
            return False
        # Only run once per (date, hour) combination
        key = task["name"]
        last = self._last_run.get(key)
        if last and last == (now.date(), now.hour):
            return False
        return True

    def _mark_ran(self, task: dict[str, Any], now: datetime) -> None:
        self._last_run[task["name"]] = (now.date(), now.hour)

    def _loop(self) -> None:
        """Main tick loop — wakes every ~30 seconds, checks schedule."""
        logger.info("[Scheduler] Tick loop started")
        while not self._stop_event.is_set():
            now = datetime.now()
            for task in self._tasks:
                if self._should_run(task, now):
                    self._mark_ran(task, now)
                    t = threading.Thread(
                        target=_run_task_safe,
                        args=(task,),
                        daemon=True,
                        name=f"sched-{task['name'][:20]}",
                    )
                    t.start()
                    logger.info(
                        "[Scheduler] Fired task '%s' at %s",
                        task["name"],
                        now.strftime("%H:%M"),
                    )
            # Sleep until the next minute boundary (±a few seconds)
            # Use a short sleep with stop_event check for responsive shutdown
            sleep_seconds = 60 - datetime.now().second
            self._stop_event.wait(timeout=max(sleep_seconds, 5))
        logger.info("[Scheduler] Tick loop stopped")


# ── Module-level singleton ────────────────────────────────────────────────────

_scheduler: Scheduler | None = None


def start(stop_event: threading.Event | None = None) -> Scheduler:
    """Create and start the global scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None and (_scheduler._thread and _scheduler._thread.is_alive()):
        logger.info("[Scheduler] Already running, skipping start")
        return _scheduler
    _scheduler = Scheduler(SCHEDULED_TASKS, stop_event=stop_event)
    _scheduler.start()
    return _scheduler


def stop() -> None:
    """Stop the global scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
