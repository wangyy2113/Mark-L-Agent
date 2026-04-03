"""Send reply messages via Feishu API."""

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetChatRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from core.config import get_settings

logger = logging.getLogger(__name__)

_client: lark.Client | None = None


def _get_client() -> lark.Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = lark.Client.builder().app_id(s.feishu_app_id).app_secret(s.feishu_app_secret).build()
    return _client


def get_chat_name(chat_id: str) -> str | None:
    """Fetch chat/group name from Feishu API. Returns None on failure."""
    try:
        req = GetChatRequest.builder().chat_id(chat_id).build()
        resp = _get_client().im.v1.chat.get(req)
        if resp.success() and resp.data:
            return resp.data.name
    except Exception:
        logger.debug("Failed to get chat name for %s", chat_id)
    return None


# Note: _preprocess_markdown removed — card JSON v2 natively supports
# headings (#), tables (|), blockquotes (>), and horizontal rules (---).

import re


_HR_RE = re.compile(r'^(-{3,}|\*{3,}|_{3,})\s*$')
_LIST_RE = re.compile(r'^[-*+] |^\d+[.)]\s')


def _is_heading(s: str) -> bool:
    return (s.startswith("#") and len(s) > 1
            and (s[1] == " " or s.startswith("##")))


def _normalize_markdown(text: str) -> str:
    """Ensure blank lines around block-level elements for Feishu card rendering.

    Feishu card markdown follows CommonMark: block elements need blank lines
    before AND after them to render correctly. LLM output often omits these,
    causing tables/headings/code to merge with surrounding text.
    """
    if not text or not text.strip():
        return text
    original = text
    lines = text.split("\n")
    result: list[str] = []
    in_code_fence = False

    for line in lines:
        stripped = line.lstrip()

        # ── Code fence toggle ──
        if stripped.startswith("```"):
            if not in_code_fence:
                # Opening fence: blank line before if needed
                if result and result[-1] != "":
                    result.append("")
            # Closing fence: no blank before (would pollute code block)
            result.append(line)
            in_code_fence = not in_code_fence
            continue

        # Inside code fence: pass through unchanged
        if in_code_fence:
            result.append(line)
            continue

        # Empty line: pass through
        if not stripped:
            result.append(line)
            continue

        # ── Check if blank line needed between prev and current ──
        if result and result[-1] != "":
            prev = result[-1].lstrip()
            needs_blank = False

            # Before: current line starts a block element
            if _is_heading(stripped):
                needs_blank = True
            elif stripped.startswith("|") and not prev.startswith("|"):
                needs_blank = True
            elif stripped.startswith(">") and not prev.startswith(">"):
                needs_blank = True
            elif _HR_RE.match(stripped):
                needs_blank = True
            elif _LIST_RE.match(stripped) and not _LIST_RE.match(prev):
                needs_blank = True

            # After: previous line ended a block element
            elif _is_heading(prev):
                needs_blank = True
            elif prev.startswith("|") and not stripped.startswith("|"):
                needs_blank = True
            elif prev.startswith(">") and not stripped.startswith(">"):
                needs_blank = True
            elif _HR_RE.match(prev):
                needs_blank = True
            elif prev.startswith("```"):
                needs_blank = True

            if needs_blank:
                result.append("")

        result.append(line)

    normalized = "\n".join(result)
    if normalized != original:
        logger.debug(
            "[normalize] changed text (%d→%d lines). First diff area: %s",
            len(lines), len(result),
            repr(normalized[:300]),
        )
    return normalized


# ── Table limit helpers (Feishu error 11310) ──────────────────────────

_CARD_TABLE_LIMIT = 5  # Feishu card v2 allows at most 5 tables per card


def _has_table(content: str) -> bool:
    """Return True if *content* contains markdown table syntax outside code fences."""
    in_fence = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            return True
    return False


def _table_to_codeblock(content: str) -> str:
    """Wrap consecutive markdown table rows in a fenced code block.

    This prevents Feishu from counting them as table components while
    preserving readability.  Non-table lines are kept as-is.
    """
    lines = content.split("\n")
    out: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            out.append("```")
            out.extend(buf)
            out.append("```")
            buf.clear()

    for line in lines:
        if line.strip().startswith("|"):
            buf.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def _limit_tables(elements: list[dict]) -> list[dict]:
    """Keep the first *_CARD_TABLE_LIMIT* tables; convert the rest to
    fenced code blocks so Feishu does not reject the card (error 11310)."""
    table_indices = [
        i for i, el in enumerate(elements)
        if _has_table(el.get("content", ""))
    ]
    if len(table_indices) <= _CARD_TABLE_LIMIT:
        return elements

    logger.info(
        "Card has %d tables (limit %d); converting %d excess tables to code blocks",
        len(table_indices), _CARD_TABLE_LIMIT,
        len(table_indices) - _CARD_TABLE_LIMIT,
    )
    excess = set(table_indices[_CARD_TABLE_LIMIT:])
    return [
        {**el, "content": _table_to_codeblock(el["content"])} if i in excess else el
        for i, el in enumerate(elements)
    ]


def _split_markdown_to_elements(text: str) -> list[dict]:
    """Split markdown into separate card elements for visual spacing.

    Feishu card v2 adds layout margins between card elements, but NOT
    between block-level items inside a single markdown element.
    Splitting at blank-line boundaries gives consistent visual spacing.

    Code fences are never split (blank lines inside code blocks are preserved).
    """
    normalized = _normalize_markdown(text)
    if not normalized or not normalized.strip():
        return [{"tag": "markdown", "content": " "}]

    elements: list[dict] = []
    current_lines: list[str] = []
    in_code_fence = False

    def _flush():
        if current_lines:
            chunk = "\n".join(current_lines).strip()
            if chunk:
                elements.append({"tag": "markdown", "content": chunk})
            current_lines.clear()

    for line in normalized.split("\n"):
        stripped = line.lstrip()

        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            current_lines.append(line)
            # Close of code fence → flush the whole block
            if not in_code_fence:
                _flush()
            continue

        if in_code_fence:
            current_lines.append(line)
            continue

        # Outside code fence: blank line = split point
        if not stripped:
            _flush()
        else:
            current_lines.append(line)

    _flush()
    elements = _limit_tables(elements)
    return elements if elements else [{"tag": "markdown", "content": " "}]


def _build_card(text: str) -> str:
    """Build a Feishu card message with markdown support (JSON v2)."""
    card = {
        "schema": "2.0",
        "body": {
            "elements": _split_markdown_to_elements(text),
        },
    }
    return json.dumps(card)


_CARD_ELEMENT_LIMIT = 150  # Feishu card v2 allows 200, leave margin


def _reply_card_once(message_id: str, text: str) -> bool:
    """Send a single card reply. Returns True on success."""
    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(_build_card(text)) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.reply(request)
    if not response.success():
        logger.error("Failed to reply: %s %s", response.code, response.msg)
        return False
    return True


def reply_text(message_id: str, text: str) -> bool:
    """Reply to a specific message using a card with markdown rendering.

    If the card would exceed Feishu's element limit, automatically splits
    into multiple reply messages.
    """
    # Feishu card markdown content limit ~30KB; truncate if needed
    if len(text) > 28000:
        text = text[:28000] + "\n\n...(内容过长已截断)"

    elements = _split_markdown_to_elements(text)
    if len(elements) <= _CARD_ELEMENT_LIMIT:
        return _reply_card_once(message_id, text)

    # Split into chunks that fit within the element limit
    logger.info("Card has %d elements, splitting into multiple messages", len(elements))
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for el in elements:
        current.append(el)
        if len(current) >= _CARD_ELEMENT_LIMIT:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    ok = True
    for chunk in chunks:
        chunk_text = "\n\n".join(el["content"] for el in chunk)
        if not _reply_card_once(message_id, chunk_text):
            ok = False
    return ok


def _build_streaming_card(
    text: str,
    status_line: str = "",
    thinking_lines: list[str] | None = None,
    is_done: bool = False,
) -> str:
    """Build a card JSON with optional status indicator line and thinking notes."""
    elements: list[dict] = []

    if status_line:
        elements.append({
            "tag": "markdown",
            "text_size": "notation",
            "content": status_line,
        })
        elements.append({"tag": "markdown", "content": "---"})

    # Thinking lines rendered as small gray text (notation size)
    if thinking_lines:
        for line in thinking_lines:
            elements.append({
                "tag": "markdown",
                "text_size": "notation",
                "content": line,
            })

    # Split main content into separate elements for visual spacing
    if text and text.strip():
        elements.extend(_split_markdown_to_elements(text))
    else:
        elements.append({"tag": "markdown", "content": " "})

    card = {
        "schema": "2.0",
        "body": {
            "elements": elements,
        },
    }
    return json.dumps(card)


def send_card(
    chat_id: str,
    text: str,
    status_line: str = "💭 Thinking...",
    thinking_lines: list[str] | None = None,
) -> str | None:
    """Send a new card message to a chat, return message_id for later patching."""
    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(_build_streaming_card(text, status_line=status_line, thinking_lines=thinking_lines)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send card: %s %s", response.code, response.msg)
        return None

    return response.data.message_id


def reply_card(
    message_id: str,
    text: str,
    status_line: str = "💭 Thinking...",
    thinking_lines: list[str] | None = None,
) -> str | None:
    """Reply to a message with a card, return message_id for later patching."""
    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(_build_streaming_card(text, status_line=status_line, thinking_lines=thinking_lines)) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.reply(request)
    if not response.success():
        logger.error("Failed to reply card: %s %s", response.code, response.msg)
        return None

    return response.data.message_id


def update_card(
    message_id: str,
    text: str,
    status_line: str = "",
    thinking_lines: list[str] | None = None,
    is_done: bool = False,
) -> bool:
    """Update an existing card message via PATCH."""


    if len(text) > 28000:
        text = text[:28000] + "\n\n...(内容过长已截断)"

    body = PatchMessageRequestBody.builder() \
        .content(_build_streaming_card(text, status_line=status_line, thinking_lines=thinking_lines, is_done=is_done)) \
        .build()

    request = PatchMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.patch(request)
    if not response.success():
        logger.error("Failed to update card: %s %s", response.code, response.msg)
        return False
    return True


def _build_header(title: str, template: str = "blue") -> dict:
    """Build a card header dict."""
    return {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    }


def reply_header_card(message_id: str, text: str, title: str, template: str = "blue") -> bool:
    """Reply with a card that has a colored header."""
    card = {
        "schema": "2.0",
        "header": _build_header(title, template),
        "body": {"elements": _split_markdown_to_elements(text)},
    }
    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(json.dumps(card)) \
        .build()
    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    response = _get_client().im.v1.message.reply(request)
    if not response.success():
        logger.error("Failed to reply header card: %s %s", response.code, response.msg)
        return False
    return True


def send_header_card(chat_id: str, text: str, title: str, template: str = "blue") -> bool:
    """Send a card with a colored header to a chat (not a reply)."""
    card = {
        "schema": "2.0",
        "header": _build_header(title, template),
        "body": {"elements": _split_markdown_to_elements(text)},
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(json.dumps(card)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()
    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send header card: %s %s", response.code, response.msg)
        return False
    return True


def send_action_card(
    chat_id: str,
    markdown_text: str,
    button_text: str,
    button_value: dict,
    header_title: str = "",
    header_template: str = "blue",
) -> str | None:
    """Send a card with an action button to a chat. Returns message_id."""
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                *_split_markdown_to_elements(markdown_text),
                {"tag": "markdown", "content": "---"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": button_text},
                    "type": "primary",
                    "behaviors": [{"type": "callback", "value": button_value}],
                },
            ],
        },
    }
    if header_title:
        card["header"] = _build_header(header_title, header_template)

    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(json.dumps(card)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send action card: %s %s", response.code, response.msg)
        return None
    return response.data.message_id


def send_select_card(
    chat_id: str,
    markdown_text: str,
    buttons: list[dict],
    header_title: str = "",
    header_template: str = "blue",
) -> str | None:
    """Send a card with multiple buttons to a chat. Returns message_id.

    Each button dict: {"text": str, "value": dict, "type": "primary"|"default"}.
    """
    columns = []
    for btn in buttons:
        columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": btn.get("type", "default"),
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            }],
        })

    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                *_split_markdown_to_elements(markdown_text),
                {"tag": "markdown", "content": "---"},
                {"tag": "column_set", "flex_mode": "flow", "columns": columns},
            ],
        },
    }
    if header_title:
        card["header"] = _build_header(header_title, header_template)

    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(json.dumps(card)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send select card: %s %s", response.code, response.msg)
        return None
    return response.data.message_id


def reply_raw_card(message_id: str, card_json: str) -> bool:
    """Reply to a message with a raw card JSON string."""
    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(card_json) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.reply(request)
    if not response.success():
        logger.error("Failed to reply raw card: %s %s", response.code, response.msg)
        return False
    return True


def send_raw_card(chat_id: str, card_json: str) -> str | None:
    """Send a raw card JSON to a chat. Returns message_id."""
    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(card_json) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send raw card: %s %s", response.code, response.msg)
        return None
    return response.data.message_id


def send_message(chat_id: str, text: str) -> bool:
    """Send a card message to a chat (not a reply)."""

    if len(text) > 28000:
        text = text[:28000] + "\n\n...(内容过长已截断)"

    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(_build_streaming_card(text, is_done=True)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _get_client().im.v1.message.create(request)
    if not response.success():
        logger.error("Failed to send message: %s %s", response.code, response.msg)
        return False
    return True
