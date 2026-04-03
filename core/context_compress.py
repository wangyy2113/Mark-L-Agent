"""Context compression: summarize long conversations to reduce input tokens.

When a chat session's input_tokens exceeds a configurable threshold (default 80k),
this module generates a concise summary using a lightweight model (haiku) and
replaces the session with the summary. On the next request, the summary is
injected into the system prompt so the model retains key context without the
full conversation history.

Design decisions:
- Uses openai-compatible API (same pattern as core/scheduler.py) for portability.
- Compression runs in a background daemon thread to avoid blocking the response.
- Rolling summaries: if a previous summary exists, it's included in the input
  so the new summary is cumulative rather than losing older context.
- Failure is safe: if summary generation fails, the session is preserved as-is.
"""

import json
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.session import SessionStore

logger = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 80_000  # input_tokens threshold to trigger compression
MAX_USER_MSG_CHARS = 3000  # truncate user message for summary input
MAX_ASSISTANT_CHARS = 5000  # truncate assistant reply for summary input

# ── Summary prompt ───────────────────────────────────────────────────────────

SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。请将以下对话内容压缩为简洁的摘要，保留：\n"
    "1. 用户的核心问题和意图\n"
    "2. 已达成的关键结论和决策\n"
    "3. 重要的技术细节（文件路径、配置值、代码片段等）\n"
    "4. 未完成的待办事项\n\n"
    "摘要控制在 1500 字以内，使用中文，结构清晰。"
)


# ── Public API ───────────────────────────────────────────────────────────────


def should_compress(input_tokens: int, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """Check if context size warrants compression."""
    return input_tokens > threshold


def generate_summary(
    user_message: str,
    assistant_reply: str,
    previous_summary: str | None = None,
    model: str = "claude-haiku-4-5",
) -> str | None:
    """Call a lightweight model to generate a conversation summary.

    Returns the summary string, or None on failure.
    """
    try:
        import openai
        from core.config import get_settings

        s = get_settings()
    except ImportError:
        logger.error("openai package not available for context compression")
        return None
    except Exception:
        logger.exception("Failed to get settings for summary generation")
        return None

    # Truncate inputs to avoid blowing up the summary call itself
    user_msg = user_message[:MAX_USER_MSG_CHARS]
    assistant_msg = assistant_reply[:MAX_ASSISTANT_CHARS]

    content_parts: list[str] = []
    if previous_summary:
        content_parts.append(f"【之前的对话摘要】\n{previous_summary}")
    content_parts.append(
        f"【最新一轮对话】\n用户：{user_msg}\n\n助手：{assistant_msg}"
    )
    content_parts.append("\n请生成更新后的完整对话摘要。")

    try:
        base_url = (
            s.anthropic_base_url.rstrip("/")
            if s.anthropic_base_url
            else "https://api.anthropic.com"
        )
        if not base_url.endswith("/v1"):
            base_url += "/v1"

        client = openai.OpenAI(api_key=s.anthropic_api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(content_parts)},
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
        result = (text or "").strip()
        if result:
            logger.info(
                "[Compress] Summary generated: %d chars (model=%s)", len(result), model
            )
            return result
        return None
    except Exception:
        logger.exception("[Compress] Summary generation failed (model=%s)", model)
        return None


def build_summary_section(summary: str) -> str:
    """Format summary for injection into system_prompt."""
    return (
        "\n\n## 之前的对话摘要\n"
        f"{summary}\n\n"
        "（以上是之前对话的压缩摘要，请基于此继续对话。）"
    )


def compress_context_async(
    chat_id: str,
    user_message: str,
    assistant_reply: str,
    session_store: "SessionStore",
    model: str = "claude-haiku-4-5",
) -> None:
    """Run compression in a background daemon thread.

    Steps:
    1. Fetch existing summary (if any) for rolling compression.
    2. Generate new summary via lightweight LLM.
    3. Clear session_id (so next request starts fresh).
    4. Store the summary (so next request can inject it).

    On failure, the session is left untouched — worst case is one more
    slow resume before compression is retried.
    """

    def _do() -> None:
        try:
            prev_summary = session_store.get_summary(chat_id)
            summary = generate_summary(
                user_message, assistant_reply, prev_summary, model=model
            )
            if summary:
                # Order matters: store summary first, then clear session.
                # This ensures the next request always has context available
                # (either via session resume or via summary injection).
                session_store.set_summary(chat_id, summary)  # store summary
                session_store.delete(chat_id)  # clear session_id
                logger.info(
                    "[Compress] Context compressed for chat=%s (%d chars)",
                    chat_id,
                    len(summary),
                )
            else:
                logger.warning(
                    "[Compress] No summary generated, keeping session for chat=%s",
                    chat_id,
                )
        except Exception:
            logger.exception("[Compress] Failed for chat=%s", chat_id)

    t = threading.Thread(
        target=_do, daemon=True, name=f"compress-{chat_id[:12]}"
    )
    t.start()
