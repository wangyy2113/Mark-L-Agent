"""Handle incoming Feishu message events."""

import json
import logging
import random
import re
import threading
import time
from pathlib import Path

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, GetMessageRequest
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
)

import agent
import core.audit as audit
from core.config import get_settings
from core.agent_session import AgentSessionStore
from core.card import (
    get_chat_name,
    reply_text,
    send_card,
    reply_card,
    reply_raw_card,
    send_raw_card,
    send_action_card,
    send_select_card,
    send_message,
    update_card,
    reply_header_card,
    send_header_card,
)
from core.permissions import Permissions
from core.session import SessionStore

logger = logging.getLogger(__name__)

# Track processed message IDs to deduplicate
_seen: set[str] = set()
_MAX_SEEN = 5000
_MAX_CARD_CHARS = 2000  # split to new card when progress text exceeds this
_THINKING_EMOJIS = [
    # 原有
    "💭",
    "🔎",
    "🧩",
    "📌",
    "💡",
    "🎯",
    "🧐",
    "📋",
    # 思考/认知
    "🤔",
    "🧠",
    "💬",
    "🪄",
    "✨",
    "🔮",
    # 观察/分析
    "👀",
    "🔬",
    "🧪",
    "📡",
    "🛰️",
    # 工作/动作
    "⚙️",
    "🔧",
    "🛠️",
    "🏗️",
    "🧵",
    "🪡",
    # 阅读/文档
    "📝",
    "📎",
    "🗂️",
    "📐",
    "🏷️",
    # 方向/导航
    "🧭",
    "🔗",
    "🪢",
    "⚡",
    # 天文/自然
    "🌀",
    "🌊",
    "🪐",
    "☄️",
    "🌙",
    "❄️",
    "🍃",
    "🌿",
    # 几何/抽象
    "◆",
    "◇",
    "▲",
    "△",
    "●",
    "○",
    "■",
    "□",
    "◉",
    "◈",
    "⬡",
    "⬢",
    # 音乐/节奏
    "🎵",
    "🎶",
    "♪",
    "♫",
    "🎧",
    # 箭头/指示
    "➤",
    "➜",
    "⟡",
    "⧫",
    "↻",
    # 极简 Unicode
    "›",
    "∴",
    "∷",
    "≋",
    "⊹",
    "⊿",
    # 复古终端风
    "▸",
    "▹",
    "░",
    "▒",
    "▓",
    # 星号/星星
    "✻",
    "⭐",
    "🌟",
    "✦",
    "✧",
    "★",
    "☆",
    "✶",
    "✸",
    "✹",
    "⁂",
    "❋",
    "❊",
    "✳",
    "✴",
    "💫",
    "🌠",
    # Braille 点阵
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
]
_THINKING_BASE = [
    "Thinking...",
    "Analyzing...",
    "Working on it...",
    "Reasoning...",
    "Looking into this...",
    "Processing...",
    "Pondering...",
    "Mulling it over...",
    "Figuring this out...",
    "Connecting the dots...",
    "Piecing it together...",
    "Investigating...",
    "Exploring...",
    "On it...",
    "One moment...",
]
_THINKING_CHAT_EXTRA = [
    "Digging in...",
    "Searching for answers...",
    "Looking things up...",
    "Cooking something up...",
    "Brewing a response...",
    "Hmm, let me see...",
    "Bear with me...",
    "Hold that thought...",
    "Give me a sec...",
    "Spinning up...",
]
_THINKING_DEV_EXTRA = [
    "Analyzing code...",
    "Reading codebase...",
    "Tracing the logic...",
    "Scanning the repo...",
    "Checking the codebase...",
    "Diving into the code...",
    "Reading the code...",
    "Following the trail...",
    "Mapping dependencies...",
    "Inspecting the source...",
]
_THINKING_CHAT_PHRASES = _THINKING_BASE + _THINKING_CHAT_EXTRA
_THINKING_DEV_PHRASES = _THINKING_BASE + _THINKING_DEV_EXTRA


def _random_thinking(dev: bool = False) -> str:
    """Pick a random thinking phrase with a random emoji prefix."""
    pool = _THINKING_DEV_PHRASES if dev else _THINKING_CHAT_PHRASES
    return f"{random.choice(_THINKING_EMOJIS)} {random.choice(pool)}"


_THINKING_SEP = "\n· · ·\n"

# Agent → card header color mapping
_AGENT_COLORS: dict[str, str] = {
    "dev": "indigo",
    "knowledge": "turquoise",
    "ops": "orange",
}

# Phase display labels (dev agent phased workflow)
_PHASE_DISPLAY: dict[str, str] = {
    "explore": "🔍 Explore",
    "planning": "📝 Planning",
    "implementing": "🛠️ Implementing",
}

_PHASE_HEADER: dict[str, str] = {
    "explore": "Dev · Explore",
    "planning": "Dev · Planning",
    "implementing": "Dev · Implementing",
}

_EXPLORE_REMINDER_INTERVAL = 5
_PLANNING_REMINDER_INTERVAL = 3

_session_store: SessionStore | None = None
_agent_session_store: AgentSessionStore | None = None
_permissions: Permissions | None = None
_usage_store = None


def init(
    session_store: SessionStore,
    agent_session_store: AgentSessionStore,
    permissions: Permissions,
    usage_store=None,
) -> None:
    global _session_store, _agent_session_store, _permissions, _usage_store
    _session_store = session_store
    _agent_session_store = agent_session_store
    _permissions = permissions
    _usage_store = usage_store


_THINKING_MAX_LEN = 300  # text longer than this is content, not thinking


def _build_status_suffix(chat_id: str, agent_state=None) -> str:
    """Build status suffix for the done status line.

    Shows agent mode, domain, and gap/new-session indicators.
    """
    if not agent_state:
        return ""

    parts = []

    # Agent mode + domain (short English name for consistent status style)
    parts.append(f"· {agent_state.agent_name.capitalize()}")
    if len(agent_state.domains) == 1:
        parts.append(f"· {agent_state.domains[0]}")

    # Phase indicator — only for dev agent (phased workflow)
    phase = getattr(agent_state, "phase", "")
    if phase and agent_state.agent_name == "dev":
        phase_label = _PHASE_DISPLAY.get(phase, phase)
        parts.append(f"| {phase_label}")

    # Gap / new session indicator
    updated_at = None
    if _agent_session_store and hasattr(_agent_session_store, "get_updated_at"):
        updated_at = _agent_session_store.get_updated_at(chat_id)

    if updated_at is not None:
        gap = time.time() - updated_at
        if gap > 86400:
            parts.append(f"· {gap / 3600:.0f}h gap")
        elif gap > 3600:
            parts.append(f"· {gap / 3600:.1f}h gap")

    return " ".join(parts)


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}min"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f}h" if h < 10 else f"{int(h)}h"
    d = seconds / 86400
    return f"{d:.1f}d" if d < 10 else f"{int(d)}d"


def _build_session_info(chat_id: str) -> str:
    """Build session info text for /session command."""
    from core.config import get_settings

    s = get_settings()
    ttl = s.session_ttl_seconds
    now = time.time()
    lines = []

    # Check agent session first
    agent_state = _agent_session_store.get(chat_id) if _agent_session_store else None
    if agent_state and agent_state.active:
        from agents import get_agent

        agent_cfg = get_agent(agent_state.agent_name)

        name = agent_state.agent_name.capitalize()
        mode_str = (
            f"{name} · {'+'.join(agent_state.domains)}" if agent_state.domains else name
        )
        phase = getattr(agent_state, "phase", "")
        if phase:
            phase_label = _PHASE_DISPLAY.get(phase, phase)
            mode_str += f" · {phase_label}"
        lines.append(f"**Mode:**    {mode_str}")

        sid = agent_state.session_id
        lines.append(f"**Session:** {sid[:12] + '...' if sid else '(none)'}")

        # Use per-agent session_ttl if configured, otherwise global
        agent_ttl = (
            agent_cfg.session_ttl if agent_cfg and agent_cfg.session_ttl > 0 else ttl
        )

        updated_at = None
        if _agent_session_store and hasattr(_agent_session_store, "get_updated_at"):
            updated_at = _agent_session_store.get_updated_at(chat_id)

        if updated_at:
            age = now - agent_state.started_at
            idle = now - updated_at
            remaining = max(0, agent_ttl - idle)
            lines.append(f"**Age:**     {_format_duration(age)}")
            lines.append(f"**Idle:**    {_format_duration(idle)}")
            lines.append(
                f"**TTL:**     {_format_duration(agent_ttl)} ({_format_duration(remaining)} left)"
            )
        return "\n".join(lines)

    # Chat session
    lines.append("**Mode:**    Chat")
    updated_at = None
    if _session_store and hasattr(_session_store, "get_updated_at"):
        updated_at = _session_store.get_updated_at(chat_id)

    sid = _session_store.get(chat_id) if _session_store else None
    lines.append(f"**Session:** {sid[:12] + '...' if sid else '(none)'}")

    if updated_at:
        idle = now - updated_at
        remaining = max(0, ttl - idle)
        lines.append(f"**Idle:**    {_format_duration(idle)}")
        lines.append(
            f"**TTL:**     {_format_duration(ttl)} ({_format_duration(remaining)} left)"
        )

    return "\n".join(lines)


def _handle_model_command(message_id: str, chat_id: str, text: str) -> None:
    """Handle /model commands: show, switch, reset."""
    from agent import (
        MODEL_ALIASES,
        get_effective_model,
        set_chat_model,
        clear_chat_model,
    )
    from agents import get_agent

    parts = text.strip().split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    # Resolve active agent config (if in an agent session)
    agent_cfg = None
    agent_state = _agent_session_store.get(chat_id) if _agent_session_store else None
    if agent_state and agent_state.active:
        agent_cfg = get_agent(agent_state.agent_name)

    if not arg:
        # Show current model + available list
        current = get_effective_model(chat_id, agent_cfg)
        # Reverse-lookup alias for display
        alias = next((a for a, mid in MODEL_ALIASES.items() if mid == current), "")
        current_display = f"{alias} ({current})" if alias else current

        model_lines = "\n".join(
            f"- **{a}** → `{mid}`" for a, mid in MODEL_ALIASES.items()
        )
        reply_text(
            message_id,
            f"**当前模型:** {current_display}\n\n可用模型：\n{model_lines}\n\n切换: `/model <名称>`\n重置: `/model reset`",
        )
        return

    if arg == "reset":
        clear_chat_model(chat_id)
        current = get_effective_model(chat_id, agent_cfg)
        alias = next((a for a, mid in MODEL_ALIASES.items() if mid == current), "")
        current_display = f"{alias} ({current})" if alias else current
        reply_text(message_id, f"已重置为默认模型: {current_display}")
        return

    resolved = set_chat_model(chat_id, arg)
    if resolved:
        alias = next((a for a, mid in MODEL_ALIASES.items() if mid == resolved), "")
        display = f"{alias} ({resolved})" if alias else resolved
        reply_text(message_id, f"模型已切换: {display}")
    else:
        valid = ", ".join(MODEL_ALIASES.keys())
        reply_text(message_id, f"未知模型: {arg}\n可用: {valid}")


def _format_thinking(text: str) -> str:
    """Format thinking text for card note element: random emoji + ▸ + trailing '...'."""
    emoji = random.choice(_THINKING_EMOJIS)
    text = text.rstrip("：:。，,、；;\n ")
    if not text.endswith("...") and not text.endswith("…"):
        text += "..."
    return f"*▸ {emoji} {text}*"


def handle_message_event(data: P2ImMessageReceiveV1) -> None:
    """Process a single message event from Feishu."""
    import main

    if main.shutting_down.is_set():
        # Best-effort reply during shutdown
        try:
            msg = data.event and data.event.message
            if msg and msg.message_id:
                reply_text(msg.message_id, "⚙️ 服务正在重启，请稍后重试。")
        except Exception:
            pass
        return

    try:
        _handle(data)
    except Exception:
        logger.exception("Error handling message event")


def _fetch_quote_content(parent_id: str) -> tuple[str, list[str], list[str]]:
    """Fetch content of a quoted (replied-to) message by its ID.

    Returns (text, image_paths, file_paths).
    """
    from core.card import _get_client
    from core.media import download_image, download_file

    try:
        req = GetMessageRequest.builder().message_id(parent_id).build()
        resp = _get_client().im.v1.message.get(req)
        if not resp.success():
            logger.warning("Failed to fetch quoted message %s: %s", parent_id, resp.msg)
            return "", [], []
        items = resp.data and resp.data.items
        if not items:
            return "", [], []
        msg = items[0]
        body_content = msg.body and msg.body.content or ""
        if not body_content:
            return "", [], []
        content = json.loads(body_content)
        msg_type = msg.msg_type or ""

        text = ""
        image_paths: list[str] = []
        file_paths: list[str] = []

        if msg_type == "text":
            text = content.get("text", "").strip()
        elif msg_type == "post":
            text = _extract_post_text(content)
            # Also extract images from post
            post = content
            if "content" not in post:
                for v in post.values():
                    if isinstance(v, dict) and "content" in v:
                        post = v
                        break
            for paragraph in post.get("content", []):
                for element in paragraph:
                    if element.get("tag") == "img":
                        ik = element.get("image_key", "")
                        if ik:
                            p = download_image(parent_id, ik)
                            if p:
                                image_paths.append(p)
        elif msg_type == "image":
            ik = content.get("image_key", "")
            if ik:
                p = download_image(parent_id, ik)
                if p:
                    image_paths.append(p)
        elif msg_type == "file":
            fk = content.get("file_key", "")
            fn = content.get("file_name", "")
            if fk:
                p = download_file(parent_id, fk, fn)
                if p:
                    file_paths.append(p)
        else:
            text = f"[{msg_type}]"

        return text, image_paths, file_paths
    except Exception:
        logger.warning("Error fetching quoted message %s", parent_id, exc_info=True)
        return "", [], []


def _handle(data: P2ImMessageReceiveV1) -> None:
    event = data.event
    if not event or not event.message:
        return

    msg = event.message
    message_id = msg.message_id
    chat_id = msg.chat_id
    chat_type = msg.chat_type  # "p2p" or "group"
    message_type = msg.message_type

    # Deduplicate
    if message_id in _seen:
        return
    _seen.add(message_id)
    if len(_seen) > _MAX_SEEN:
        _seen.clear()

    # Extract sender_id
    sender_id = ""
    if event.sender and event.sender.sender_id:
        sender_id = event.sender.sender_id.open_id or ""

    # Parse text content based on message type
    try:
        content = json.loads(msg.content)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse message content")
        return

    if message_type == "text":
        text = content.get("text", "").strip()
    elif message_type == "post":
        # Rich text: extract plain text from all paragraphs
        text = _extract_post_text(content)
    elif message_type in ("image", "file"):
        text = ""  # Pure image/file, no text content
    else:
        logger.info("Skipping unsupported message type: %s", message_type)
        return

    # Extract and download media (images/files) from the message
    image_paths, file_paths = _extract_media(message_type, content, message_id)
    has_media = bool(image_paths or file_paths)

    if not text and not has_media:
        return

    # Collect mentions before stripping them from text
    mentions = msg.mentions or []
    if mentions:
        for _m in mentions:
            logger.debug(
                "mention: key=%r name=%r id=%r open_id=%r",
                getattr(_m, "key", ""),
                getattr(_m, "name", ""),
                getattr(_m, "id", ""),
                getattr(getattr(_m, "id", None), "open_id", ""),
            )

    # In group chats, only respond when @mentioned
    if chat_type == "group":
        if not mentions:
            return

    # Clean @mention placeholders from text (applies to both group and p2p).
    # Two formats exist:
    #   - text messages: use @_user_N placeholders (matched by key)
    #   - post messages: use @DisplayName (matched by name)
    # We clean both to ensure commands like /dev are correctly recognized.
    _bot_open_id = get_settings().bot_open_id
    if mentions:
        for m in mentions:
            key = getattr(m, "key", "")
            id_field = getattr(m, "id", None)
            open_id = ""
            if id_field is not None:
                if hasattr(id_field, "open_id"):
                    open_id = id_field.open_id or ""
                elif isinstance(id_field, str):
                    open_id = id_field
            name = getattr(m, "name", "") or ""
            # Identify bot: match configured bot_open_id, or non-ou_ id
            is_bot = (
                (open_id == _bot_open_id)
                if _bot_open_id
                else not open_id.startswith("ou_")
            )
            if is_bot:
                # Bot — remove entirely
                if key:
                    text = text.replace(key, "")
                if name:
                    text = text.replace(f"@{name}", "")
            elif name:
                # Real user — replace placeholder with display name
                if key:
                    text = text.replace(key, name)
                text = text.replace(f"@{name}", name)

    # Fallback: strip any remaining @_user_N placeholders
    text = re.sub(r"@_user_\d+", "", text).strip()

    if not text and not has_media:
        reply_raw_card(message_id, _build_welcome_card_json())
        return

    # ── Permission check ──
    if _permissions and not _permissions.is_allowed(sender_id, chat_id):
        audit.log_denied(sender_id, chat_id, "not in whitelist")
        logger.info("Denied message from sender=%s chat=%s", sender_id, chat_id)
        return  # silently ignore

    group = _permissions.get_group(sender_id, chat_id) if _permissions else "admin"
    is_admin = group == "admin"

    # ── Special commands ──
    # In group chats, mention remnants may precede the command
    # (e.g. "BotName /dev list" when bot open_id has ou_ prefix).
    # Extract the /command portion so it's recognized correctly.
    text_lower = text.lower().strip()
    if chat_type == "group" and not text_lower.startswith("/"):
        # Build dynamic command pattern from global commands + registered agents
        from agents import list_agents

        agent_cmds = "|".join(a.command.lstrip("/") for a in list_agents() if a.command)
        builtin = "stop|clear|help|admin|agent|session|model"
        pattern = f"/({builtin}|{agent_cmds})\\b" if agent_cmds else f"/({builtin})\\b"
        cmd_match = re.search(pattern, text_lower)
        if cmd_match:
            text = text[cmd_match.start() :]
            text_lower = text.lower().strip()

    if text_lower == "/stop":
        cancelled = agent.cancel_agent(chat_id)
        if not cancelled:
            reply_text(message_id, "当前没有进行中的请求。")
        # If cancelled, the streaming card will update to "已停止" automatically.
        return

    if text_lower == "/clear":
        if _session_store:
            _session_store.clear_all(chat_id)
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            _agent_session_store.clear_session_id(chat_id)
        # Clear sudo override
        if _permissions:
            _permissions.clear_sudo(chat_id)
        reply_text(message_id, "会话已清除，下次消息将开始新对话。")
        return

    if text_lower == "/help":
        reply_raw_card(message_id, _build_help_card_json())
        return

    if text_lower == "/session":
        reply_text(message_id, _build_session_info(chat_id))
        return

    if text_lower.startswith("/model"):
        _handle_model_command(message_id, chat_id, text)
        return

    if text_lower.startswith("/sudo"):
        _handle_sudo_command(message_id, chat_id, sender_id, text, is_admin)
        return

    if text_lower.startswith("/admin"):
        _handle_admin_command(message_id, chat_id, sender_id, text, mentions, is_admin)
        return

    # /agent global commands
    if text_lower.startswith("/agent"):
        _handle_agent_global_command(message_id, chat_id, sender_id, text, group)
        return

    # Dynamic agent command lookup from registry
    from agents import find_by_command

    cmd_word = text_lower.split()[0] if text_lower.startswith("/") else ""
    agent_cfg = find_by_command(cmd_word) if cmd_word else None
    if agent_cfg:
        _handle_agent_command(agent_cfg, message_id, chat_id, sender_id, text, group)
        return

    # ── Compose media & quote context (only for non-command messages) ──
    if has_media:
        text = _compose_prompt_with_media(text, image_paths, file_paths)
        logger.debug("Composed media prompt (%d chars): %s", len(text), text[:200])

    parent_id = msg.parent_id or ""
    if parent_id:
        quote_text, quote_images, quote_files = _fetch_quote_content(parent_id)
        if quote_text or quote_images or quote_files:
            quote_parts = ["[用户引用了一条消息]"]
            if quote_text:
                quote_parts.append(f"> {quote_text}")
            if quote_images or quote_files:
                media_text = _compose_prompt_with_media("", quote_images, quote_files)
                quote_parts.append(f"[引用消息附件]{media_text}")
            text = "\n".join(quote_parts) + "\n\n" + text

    # Active agent session: forward normal messages
    if _agent_session_store and _agent_session_store.is_active(chat_id):
        state = _agent_session_store.get(chat_id)
        if state and state.agent_name == "dev":
            _handle_dev_message(message_id, chat_id, sender_id, text, group)
        elif state:
            _handle_agent_message(
                state.agent_name, message_id, chat_id, sender_id, text, group
            )
        return

    # ── Orchestrator mode (role agent) ──
    if _is_orchestrator_mode(chat_id):
        _handle_orchestrator_message(message_id, chat_id, sender_id, text, group)
        return

    # ── Daily budget gate ──
    budget_msg = agent.check_daily_budget(sender_id)
    if budget_msg:
        reply_text(message_id, budget_msg)
        return

    # ── Normal message → Agent ──
    logger.info(
        "Processing message from chat=%s sender=%s: %s", chat_id, sender_id, text[:80]
    )

    # Reply with placeholder card immediately
    card_msg_id = reply_card(message_id, " ")  # note already shows status

    if card_msg_id:
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        last_status: str = _random_thinking()
        _last_card_update: float = 0.0

        def _append_text(text_so_far: str) -> None:
            if len(text_so_far) <= _THINKING_MAX_LEN and not content_parts:
                thinking_parts.append(_format_thinking(text_so_far))
            elif len(text_so_far) <= _THINKING_MAX_LEN:
                content_parts.append(_format_thinking(text_so_far))
            else:
                content_parts.append(text_so_far)

        def _build_body() -> str:
            return _THINKING_SEP.join(content_parts) if content_parts else " "

        def on_progress(text_so_far: str, status_line: str, is_done: bool) -> None:
            nonlocal card_msg_id, last_status, _last_card_update

            # Throttle card updates: min 1s interval (except is_done which always fires)
            now = time.monotonic()
            if not is_done and (now - _last_card_update) < 1.0:
                # Still capture state changes, just skip the API call
                if text_so_far and not status_line:
                    _append_text(text_so_far)
                elif status_line:
                    last_status = status_line
                return

            if is_done and status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line
            elif text_so_far and not status_line:
                _append_text(text_so_far)
                body = _build_body()
                # Split to new card if body too long
                if len(body) > _MAX_CARD_CHARS and len(content_parts) > 1:
                    carry = 1
                    if len(content_parts) > 2 and len(content_parts[-2]) < 200:
                        carry = 2
                    old_body = _THINKING_SEP.join(content_parts[:-carry])
                    update_card(
                        card_msg_id,
                        old_body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    content_parts[:] = content_parts[-carry:]
                    thinking_parts.clear()
                    body = _build_body()
                    new_id = send_card(
                        chat_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    if new_id:
                        card_msg_id = new_id
                    else:
                        update_card(
                            card_msg_id,
                            body,
                            status_line=last_status,
                            thinking_lines=thinking_parts,
                        )
                else:
                    update_card(
                        card_msg_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
            elif status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line

            _last_card_update = time.monotonic()

        try:
            response = agent.run(
                chat_id, text, on_progress=on_progress, group=group, sender_id=sender_id
            )
            if response != "（已停止）":
                if thinking_parts or content_parts:
                    reply_text(message_id, response)
                else:
                    update_card(card_msg_id, response, status_line=last_status)
        except TimeoutError:
            update_card(card_msg_id, "⚠️ 该会话正忙，请稍后再试。")
        except Exception as e:
            logger.exception("Agent error for chat=%s", chat_id)
            update_card(card_msg_id, _error_message(e))
    else:
        # Fallback: send_card failed, use reply_text
        try:
            response = agent.run(chat_id, text, group=group, sender_id=sender_id)
            if response != "（已停止）":
                reply_text(message_id, response)
        except Exception as e:
            logger.exception("Agent error for chat=%s", chat_id)
            reply_text(message_id, _error_message(e))


# ── Orchestrator mode ──


def _is_orchestrator_mode(chat_id: str) -> bool:
    """Check if the orchestrator (role agent) should handle this message.

    Returns True when config.yaml has default_mode=role.
    """
    from agents import get_default_mode

    return get_default_mode() == "role"


def _handle_orchestrator_message(
    message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Handle a message in orchestrator mode — forward to role agent."""
    budget_msg = agent.check_daily_budget(sender_id)
    if budget_msg:
        reply_text(message_id, budget_msg)
        return

    logger.info(
        "Orchestrator message from chat=%s sender=%s: %s", chat_id, sender_id, text[:80]
    )

    _init_status = _random_thinking()
    card_msg_id = reply_card(message_id, " ", status_line=_init_status)

    if card_msg_id:
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        last_status: str = _init_status
        _last_card_update: float = 0.0

        def _append_text(text_so_far: str) -> None:
            if len(text_so_far) <= _THINKING_MAX_LEN and not content_parts:
                thinking_parts.append(_format_thinking(text_so_far))
            elif len(text_so_far) <= _THINKING_MAX_LEN:
                content_parts.append(_format_thinking(text_so_far))
            else:
                content_parts.append(text_so_far)

        def _build_body() -> str:
            return _THINKING_SEP.join(content_parts) if content_parts else " "

        def on_progress(text_so_far: str, status_line: str, is_done: bool) -> None:
            nonlocal card_msg_id, last_status, _last_card_update

            now = time.monotonic()
            if not is_done and (now - _last_card_update) < 1.0:
                if text_so_far and not status_line:
                    _append_text(text_so_far)
                elif status_line:
                    last_status = status_line
                return

            if is_done and status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line
            elif text_so_far and not status_line:
                _append_text(text_so_far)
                body = _build_body()
                if len(body) > _MAX_CARD_CHARS and len(content_parts) > 1:
                    carry = 1
                    if len(content_parts) > 2 and len(content_parts[-2]) < 200:
                        carry = 2
                    old_body = _THINKING_SEP.join(content_parts[:-carry])
                    update_card(
                        card_msg_id,
                        old_body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    content_parts[:] = content_parts[-carry:]
                    thinking_parts.clear()
                    body = _build_body()
                    new_id = send_card(
                        chat_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    if new_id:
                        card_msg_id = new_id
                    else:
                        update_card(
                            card_msg_id,
                            body,
                            status_line=last_status,
                            thinking_lines=thinking_parts,
                        )
                else:
                    update_card(
                        card_msg_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
            elif status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line

            _last_card_update = time.monotonic()

        try:
            response = agent.run_orchestrator(
                chat_id, text, on_progress=on_progress, group=group, sender_id=sender_id
            )
            if response != "（已停止）":
                if thinking_parts or content_parts:
                    reply_text(message_id, response)
                else:
                    update_card(card_msg_id, response, status_line=last_status)
        except TimeoutError:
            update_card(card_msg_id, "⚠️ 该会话正忙，请稍后再试。")
        except Exception as e:
            logger.exception("Orchestrator error for chat=%s", chat_id)
            update_card(card_msg_id, _error_message(e))
    else:
        try:
            response = agent.run_orchestrator(
                chat_id, text, group=group, sender_id=sender_id
            )
            if response != "（已停止）":
                reply_text(message_id, response)
        except Exception as e:
            logger.exception("Orchestrator error for chat=%s", chat_id)
            reply_text(message_id, _error_message(e))


# ── Permission helpers ──


def _check_agent_access(group: str, agent_name: str) -> bool:
    """Check if a permission group allows access to an agent mode."""
    if group == "admin":
        return True
    if not _permissions:
        return True
    cfg = _permissions.get_group_config(group)
    if not cfg:
        return False
    # Check by agent name ("dev", "ask") or command ("/dev", "/ask")
    from agents import get_agent

    agent_cfg = get_agent(agent_name)
    cmd = agent_cfg.command.lstrip("/") if agent_cfg else agent_name
    return agent_name in cfg.agents or cmd in cfg.agents


# ── /sudo command (admin testing) ──


def _handle_sudo_command(
    message_id: str, chat_id: str, sender_id: str, text: str, is_admin: bool
) -> None:
    """Handle /sudo command for admin permission group testing."""
    # Check real identity, not sudo-overridden group — otherwise /sudo off
    # is blocked when simulating a non-admin group.
    real_admin = _permissions.is_admin(sender_id) if _permissions else is_admin
    if not real_admin:
        reply_text(message_id, "⚠️ 仅管理员可使用 /sudo 命令。")
        return

    if not _permissions:
        reply_text(message_id, "权限模块未初始化。")
        return

    parts = text.strip().split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg or arg == "help":
        current = _permissions.get_sudo(chat_id)
        groups = _permissions.list_group_configs()
        group_names = ", ".join(groups.keys())
        status = f"当前模拟：**{current}**" if current else "未激活"
        reply_text(
            message_id,
            f"**权限模拟（/sudo）**\n\n{status}\n\n"
            f"`/sudo <组名>` — 模拟权限组\n"
            f"`/sudo off` — 恢复 admin\n\n"
            f"可用组：{group_names}",
        )
        return

    if arg == "off":
        _permissions.clear_sudo(chat_id)
        reply_text(message_id, "已恢复 admin 权限。")
        audit.log_admin_action(sender_id, "sudo-off", chat_id)
        return

    # Validate group name
    group_cfg = _permissions.get_group_config(arg)
    if not group_cfg:
        groups = _permissions.list_group_configs()
        reply_text(message_id, f"未知权限组：{arg}\n可用组：{', '.join(groups.keys())}")
        return

    _permissions.set_sudo(chat_id, arg)
    reply_text(
        message_id, f"已切换为 **{arg}** 权限组（测试模式）。\n发 `/sudo off` 恢复。"
    )
    audit.log_admin_action(sender_id, "sudo", f"{arg} in {chat_id}")


# ── /admin command handling ──


def _handle_admin_command(
    message_id: str,
    chat_id: str,
    sender_id: str,
    text: str,
    mentions: list,
    is_admin: bool,
) -> None:
    """Handle /admin subcommands."""
    parts = text.strip().split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else "help"

    if not is_admin:
        reply_text(message_id, "⚠️ 仅管理员可使用 /admin 命令。")
        audit.log_denied(sender_id, chat_id, "non-admin tried /admin")
        return

    if sub == "help":
        reply_text(message_id, _ADMIN_HELP_TEXT)
        return

    if sub == "list":
        _admin_list(message_id, sender_id)
        return

    if sub == "role":
        _handle_admin_role(message_id, chat_id, sender_id, text, mentions)
        return

    if sub == "group":
        _handle_admin_group(message_id, chat_id, sender_id, text, mentions)
        return

    if sub == "chat-group":
        _handle_admin_chat_group(message_id, chat_id, sender_id, text)
        return

    if sub == "stats":
        _handle_admin_stats(message_id)
        return

    if sub == "log":
        _handle_admin_log(message_id, text)
        return

    if sub == "status":
        _handle_admin_status(message_id)
        return

    if sub == "sessions":
        _handle_admin_sessions(message_id, chat_id, text)
        return

    reply_text(message_id, f"未知子命令: {sub}\n使用 /admin help 查看帮助。")


def _get_mention_open_ids(mentions: list) -> list[tuple[str, str]]:
    """Extract (open_id, name) pairs from mentions, excluding non-user mentions."""
    result = []
    for m in mentions:
        name = getattr(m, "name", "") or "unknown"

        # MentionEvent.id is a UserId object with .open_id attribute,
        # but Mention.id is a plain string. Handle both.
        id_field = getattr(m, "id", None)
        open_id = ""
        if id_field is not None:
            if hasattr(id_field, "open_id"):
                # UserId object
                open_id = id_field.open_id or ""
            elif isinstance(id_field, str):
                open_id = id_field

        if open_id and open_id.startswith("ou_"):
            result.append((open_id, name))
    return result


def _admin_list(message_id: str, sender_id: str) -> None:
    if not _permissions:
        reply_text(message_id, "权限模块未初始化。")
        return
    data = _permissions.list_all()
    lines = ["**权限分配**\n"]
    lines.append(f"**admin** ({len(data['admins'])}):")
    for oid in data["admins"]:
        lines.append(f"  - `{oid}`")
    for group_name, members in data["groups"].items():
        lines.append(f"\n**{group_name}** ({len(members)}):")
        for oid in members:
            lines.append(f"  - `{oid}`")
    lines.append(f"\n**群默认组** ({len(data['chats'])}):")
    for cid, gname in data["chats"].items():
        lines.append(f"  - `{cid}` → {gname}")
    if not data["admins"] and not data["groups"] and not data["chats"]:
        lines.append("\n当前无任何权限配置，所有请求将被拒绝。请先添加 admin。")
    reply_text(message_id, "\n".join(lines))
    audit.log_admin_action(sender_id, "list", "listed permissions")


def _handle_admin_group(
    message_id: str, chat_id: str, sender_id: str, text: str, mentions: list
) -> None:
    """Handle /admin group subcommands: assign user to group, list groups."""
    if not _permissions:
        reply_text(message_id, "权限模块未初始化。")
        return

    # Parse: /admin group [list|@user group_name|@user remove]
    parts = text.strip().split(None, 2)  # ["/admin", "group", rest]
    rest = parts[2].strip() if len(parts) > 2 else ""
    rest_lower = rest.lower()

    if not rest or rest_lower == "list":
        data = _permissions.list_all()
        configs = _permissions.list_group_configs()
        lines = ["**权限组分配**\n"]
        lines.append(
            f"**admin** ({len(data['admins'])}): "
            + ", ".join(f"`{oid}`" for oid in data["admins"])
        )
        for group_name in configs:
            if group_name == "admin":
                continue
            members = data["groups"].get(group_name, [])
            lines.append(
                f"**{group_name}** ({len(members)}): "
                + (", ".join(f"`{oid}`" for oid in members) or "无")
            )
        reply_text(message_id, "\n".join(lines))
        return

    targets = _get_mention_open_ids(mentions)
    # Filter to targets whose name appears in rest
    targets = [(oid, name) for oid, name in targets if name in rest] if rest else []

    if not targets:
        configs = _permissions.list_group_configs()
        group_names = ", ".join(configs.keys())
        reply_text(
            message_id,
            f"请在命令中 @要操作的用户。\n例如: `/admin group @某某 developer`\n\n可用组：{group_names}",
        )
        return

    # Check for "remove" keyword
    if "remove" in rest_lower:
        results = []
        for open_id, name in targets:
            prev = _permissions.remove_from_group(open_id)
            if prev:
                results.append(f"已移除 {name} 的权限（原组：{prev}）")
            else:
                results.append(f"{name} 未在任何组中")
            audit.log_admin_action(sender_id, "group-remove", f"{name} ({open_id})")
        reply_text(message_id, "\n".join(results))
        return

    # Extract group name: remove @mention parts from rest
    group_name = rest
    for _, name in targets:
        group_name = group_name.replace(name, "").strip()
    group_name = group_name.strip().lower()

    if not group_name:
        configs = _permissions.list_group_configs()
        reply_text(
            message_id, f"请指定权限组名称。\n可用组：{', '.join(configs.keys())}"
        )
        return

    results = []
    for open_id, name in targets:
        if _permissions.set_group(open_id, group_name):
            results.append(f"已将 {name} 设置为 **{group_name}** 组")
        else:
            results.append(f"未知权限组：{group_name}")
        audit.log_admin_action(
            sender_id, "group-set", f"{name} ({open_id}) → {group_name}"
        )
    reply_text(message_id, "\n".join(results))


def _handle_admin_chat_group(
    message_id: str, chat_id: str, sender_id: str, text: str
) -> None:
    """Handle /admin chat-group: set default group for current chat."""
    if not _permissions:
        reply_text(message_id, "权限模块未初始化。")
        return

    parts = text.strip().split(None, 2)
    group_name = parts[2].strip().lower() if len(parts) > 2 else ""

    if not group_name:
        current = _permissions.list_all()["chats"].get(chat_id)
        configs = _permissions.list_group_configs()
        status = f"当前群默认组：**{current}**" if current else "未设置"
        reply_text(
            message_id,
            f"{status}\n\n`/admin chat-group <组名>` — 设置默认组\n"
            f"`/admin chat-group remove` — 移除默认组\n\n"
            f"可用组：{', '.join(configs.keys())}",
        )
        return

    if group_name == "remove":
        _permissions.remove_chat(chat_id)
        reply_text(message_id, f"已移除当前群的默认组: `{chat_id}`")
        audit.log_admin_action(sender_id, "chat-group-remove", chat_id)
        return

    if _permissions.set_chat_group(chat_id, group_name):
        reply_text(message_id, f"已将当前群默认组设为 **{group_name}**: `{chat_id}`")
        audit.log_admin_action(sender_id, "chat-group-set", f"{chat_id} → {group_name}")
    else:
        configs = _permissions.list_group_configs()
        reply_text(
            message_id, f"未知权限组：{group_name}\n可用组：{', '.join(configs.keys())}"
        )


def _handle_admin_stats(message_id: str) -> None:
    """Handle /admin stats — usage overview."""
    if not _usage_store:
        reply_text(message_id, "用量模块未初始化。")
        return

    summary = _usage_store.query_daily_summary()
    by_agent = _usage_store.query_by_agent(days=1)
    by_day = _usage_store.query_by_day(days=7)

    lines = ["**今日用量**\n"]
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 请求数 | {summary['count']} |")
    lines.append(f"| 总成本 | ${summary['cost_usd']:.2f} |")
    lines.append(f"| 总轮次 | {summary['turns']} |")
    lines.append(f"| 输入 tokens | {summary['input_tokens']:,} |")
    lines.append(f"| 输出 tokens | {summary['output_tokens']:,} |")

    if by_agent:
        lines.append("\n**按 Agent 分组（今日）**\n")
        lines.append("| Agent | 请求 | 成本 | 轮次 |")
        lines.append("|-------|------|------|------|")
        for a in by_agent:
            lines.append(
                f"| {a['agent']} | {a['count']} | ${a['cost_usd']:.2f} | {a['turns']} |"
            )

    if by_day:
        lines.append("\n**最近 7 天趋势**\n")
        lines.append("| 日期 | 请求 | 成本 |")
        lines.append("|------|------|------|")
        for d in by_day:
            # Show MM-DD format
            date_str = (
                d["date"][5:] if d["date"] and len(d["date"]) >= 10 else d["date"]
            )
            lines.append(f"| {date_str} | {d['count']} | ${d['cost_usd']:.2f} |")

    reply_text(message_id, "\n".join(lines))


def _handle_admin_log(message_id: str, text: str) -> None:
    """Handle /admin log [N] — recent request details."""
    if not _usage_store:
        reply_text(message_id, "用量模块未初始化。")
        return

    # Parse limit from text: "/admin log 10" → 10
    parts = text.strip().split()
    limit = 20
    if len(parts) >= 3:
        try:
            limit = max(1, min(int(parts[2]), 100))
        except ValueError:
            pass

    rows = _usage_store.query_recent(limit)
    if not rows:
        reply_text(message_id, "暂无请求记录。")
        return

    lines = [f"**最近 {len(rows)} 条请求**\n"]
    lines.append("| # | 时间 | Agent | 费用 | 轮次 | 工具 | 耗时 | out |")
    lines.append("|---|------|-------|------|------|------|------|-----|")
    for i, r in enumerate(rows, 1):
        ts = time.strftime("%H:%M", time.localtime(r["timestamp"]))
        agent = r["agent_name"] or "chat"
        cost = f"${r['cost_usd']:.2f}"
        dur = f"{r['duration_s']:.1f}s"
        lines.append(
            f"| {i} | {ts} | {agent} | {cost} | {r['sdk_turns']} | {r['tool_count']} | {dur} | {r['output_tokens']} |"
        )

    reply_text(message_id, "\n".join(lines))


def _handle_admin_status(message_id: str) -> None:
    """Handle /admin status — service status overview."""
    import resource
    import main as _main
    import core.mcp as _mcp
    from core.config import get_settings as _get_settings

    settings = _get_settings()

    # Uptime
    uptime_s = time.time() - _main.get_start_time()
    days = int(uptime_s // 86400)
    hours = int((uptime_s % 86400) // 3600)
    minutes = int((uptime_s % 3600) // 60)
    if days > 0:
        uptime_str = f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        uptime_str = f"{hours}h {minutes}m"
    else:
        uptime_str = f"{minutes}m"

    # MCP servers
    mcp_servers = _mcp._mcp_servers or {}
    mcp_names = list(mcp_servers.keys())
    mcp_str = f"{', '.join(mcp_names)} ({len(mcp_names)})" if mcp_names else "无"

    # Active sessions
    chat_count = (
        _session_store.count_active()
        if _session_store and hasattr(_session_store, "count_active")
        else "N/A"
    )
    agent_sessions = []
    if _agent_session_store and hasattr(_agent_session_store, "list_active"):
        agent_sessions = _agent_session_store.list_active()
    agent_count = len(agent_sessions)
    if agent_sessions:
        details = ", ".join(
            f"{s['type']}·{'|'.join(s['domains']) or '?'}" for s in agent_sessions
        )
        agent_str = f"{agent_count} ({details})"
    else:
        agent_str = "0"

    # Memory (ru_maxrss: bytes on macOS, KB on Linux)
    import sys as _sys

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if _sys.platform == "darwin":
        mem_mb = rss / (1024 * 1024)
    else:
        mem_mb = rss / 1024
    mem_str = f"{mem_mb:.1f} MB"

    # DB size
    db_path = Path(settings.session_db_path)
    if db_path.exists():
        db_bytes = db_path.stat().st_size
        if db_bytes >= 1024 * 1024:
            db_str = f"{db_bytes / (1024 * 1024):.1f} MB"
        else:
            db_str = f"{db_bytes / 1024:.1f} KB"
    else:
        db_str = "N/A"

    # API key usage (bridge only, silently skip on failure)
    key_usage_str = ""
    base_url = settings.anthropic_base_url
    if base_url:
        try:
            import urllib.request
            from urllib.parse import urlparse

            parsed = urlparse(base_url)
            usage_url = f"{parsed.scheme}://{parsed.netloc}/usage"
            req = urllib.request.Request(
                usage_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.anthropic_api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            pct = data.get("usage_percent", 0.0)
            has_budget = data.get("has_budget", False)
            if has_budget:
                key_usage_str = f"{pct:.1f}% 已使用"
            else:
                key_usage_str = "无额度限制"
        except Exception:
            key_usage_str = ""

    lines = ["**服务状态**\n"]
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 运行时长 | {uptime_str} |")
    if key_usage_str:
        lines.append(f"| API Key 用量 | {key_usage_str} |")
    lines.append(f"| MCP 服务器 | {mcp_str} |")
    lines.append(f"| 活跃 Chat 会话 | {chat_count} |")
    lines.append(f"| 活跃 Agent 会话 | {agent_str} |")
    lines.append(f"| 内存占用 | {mem_str} |")
    lines.append(f"| 数据库大小 | {db_str} |")

    reply_text(message_id, "\n".join(lines))


def _handle_admin_sessions(message_id: str, chat_id: str, text: str) -> None:
    """Handle /admin sessions [clear <chat_id>] — session management."""
    parts = text.strip().split()

    # /admin sessions clear <chat_id>
    if len(parts) >= 4 and parts[2].lower() == "clear":
        target_chat = parts[3]
        cleared = []
        if _session_store:
            _session_store.clear_all(target_chat)
            cleared.append("chat")
        if _agent_session_store:
            _agent_session_store.deactivate(target_chat)
            cleared.append("agent")
        reply_text(
            message_id, f"已清除 `{target_chat}` 的会话（{', '.join(cleared)}）。"
        )
        return

    # Default: list active sessions
    lines = ["**会话概览**\n"]

    chat_count = (
        _session_store.count_active()
        if _session_store and hasattr(_session_store, "count_active")
        else "N/A"
    )
    lines.append(f"活跃 Chat 会话: **{chat_count}**\n")

    agent_sessions = []
    if _agent_session_store and hasattr(_agent_session_store, "list_active"):
        agent_sessions = _agent_session_store.list_active()

    if agent_sessions:
        # Resolve chat names from Feishu API (deduplicate by chat_id)
        unique_cids = {s["chat_id"] for s in agent_sessions}
        chat_names: dict[str, str] = {}
        for cid in unique_cids:
            name = get_chat_name(cid)
            if name:
                chat_names[cid] = name

        lines.append(f"**活跃 Agent 会话 ({len(agent_sessions)})**\n")
        lines.append("| 会话 | 类型 | 域 | 阶段 | Chat ID |")
        lines.append("|------|------|----|------|---------|")
        for s in agent_sessions:
            cid = s["chat_id"]
            name = chat_names.get(cid, "-")
            domains = "|".join(s["domains"]) or "-"
            lines.append(f"| {name} | {s['type']} | {domains} | {s['state']} | {cid} |")
    else:
        lines.append("无活跃 Agent 会话。")

    lines.append(f"\n`/admin sessions clear <chat_id>` — 清除指定会话")

    reply_text(message_id, "\n".join(lines))


def _handle_admin_role(
    message_id: str, chat_id: str, sender_id: str, text: str, mentions: list
) -> None:
    """Handle /admin role subcommands: set, list, remove."""
    if not _permissions:
        reply_text(message_id, "权限模块未初始化。")
        return

    # Parse: /admin role [list|remove @user|@user description...]
    # text is the full command, e.g. "/admin role @张三 后端研发"
    # In group chats, text has been cleaned: bot name stripped, user names injected.
    # But mentions list still includes the bot. Filter to only mentions whose
    # name appears in the command text (bot name was removed by command extraction).
    parts = text.strip().split(None, 2)  # ["/admin", "role", rest]
    rest = parts[2].strip() if len(parts) > 2 else ""
    rest_lower = rest.lower()

    # Filter mentions: only keep those whose name appears in rest (excludes bot trigger)
    all_targets = _get_mention_open_ids(mentions)
    targets = [(oid, name) for oid, name in all_targets if name in rest] if rest else []

    if not rest or rest_lower == "list":
        # /admin role list
        roles = _permissions.get_roles(chat_id)
        if not roles:
            reply_text(
                message_id,
                "当前群未设置任何角色。\n使用 `/admin role @某某 角色描述` 设置。",
            )
        else:
            lines = [f"**当前群角色 ({len(roles)} 人)**\n"]
            for oid, info in roles.items():
                lines.append(f"- {info['name']}: {info['desc']}")
            reply_text(message_id, "\n".join(lines))
        return

    if rest_lower.startswith("remove"):
        # /admin role remove @user
        if not targets:
            reply_text(
                message_id,
                "请在命令中 @要移除角色的用户。\n例如: `/admin role remove @某某`",
            )
            return
        results = []
        for open_id, name in targets:
            if _permissions.remove_role(chat_id, open_id):
                results.append(f"已移除角色: {name}")
            else:
                results.append(f"{name} 未设置角色")
            audit.log_admin_action(sender_id, "role-remove", f"{name} ({open_id})")
        reply_text(message_id, "\n".join(results))
        return

    # /admin role @user description
    if not targets:
        reply_text(
            message_id,
            "请在命令中 @要设置角色的用户。\n例如: `/admin role @某某 后端研发`",
        )
        return

    # Extract description: remove @mention placeholders and "role" keyword from rest
    desc = rest
    for m in mentions:
        key = getattr(m, "key", "")
        name = getattr(m, "name", "") or ""
        if key:
            desc = desc.replace(key, "")
        if name:
            desc = desc.replace(f"@{name}", "")
    desc = desc.strip()

    if not desc:
        reply_text(
            message_id, "请提供角色描述。\n例如: `/admin role @某某 后端研发-支付模块`"
        )
        return

    results = []
    for open_id, name in targets:
        _permissions.set_role(chat_id, open_id, name, desc)
        results.append(f"已设置角色: {name} → {desc}")
        audit.log_admin_action(sender_id, "role-set", f"{name} ({open_id}): {desc}")
    reply_text(message_id, "\n".join(results))


# ── /agent global commands ──


def _handle_agent_global_command(
    message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Handle /agent list, /agent done, etc."""
    parts = text.strip().split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else "list"

    if arg == "list":
        from agents import list_agents

        agents = list_agents()
        if not agents:
            reply_text(message_id, "当前没有已加载的 Agent。仅支持普通聊天模式。")
            return
        lines = ["**已注册的 Agent：**\n"]
        for a in agents:
            lines.append(f"- **{a.display_name}** (`{a.command}`) — {a.description}")
        reply_text(message_id, "\n".join(lines))
        return

    if arg == "done":
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            state = _agent_session_store.get(chat_id)
            agent_name = state.agent_name if state else "unknown"
            _cleanup_before_deactivate(chat_id)
            _agent_session_store.deactivate(chat_id)
            from agents import get_agent

            cfg = get_agent(agent_name)
            display = cfg.display_name if cfg else agent_name
            reply_text(message_id, f"已退出「{display}」模式，回到普通聊天。")
            audit.log_admin_action(
                sender_id, "agent-done", f"{agent_name} in {chat_id}"
            )
        else:
            reply_text(message_id, "当前未在任何 Agent 模式中。")
        return

    if arg == "help":
        lines = ["**Agent 管理命令：**\n"]
        lines.append("/agent list — 列出所有已注册的 Agent")
        lines.append("/agent done — 退出当前 Agent 模式")
        lines.append("/agent help — 显示此帮助")
        reply_text(message_id, "\n".join(lines))
        return

    reply_text(message_id, f"未知 /agent 子命令: {arg}\n使用 `/agent help` 查看帮助。")


# ── Agent command dispatch ──


def _handle_agent_command(
    agent_cfg, message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Dispatch to the appropriate agent command handler based on registry config."""
    if agent_cfg.name == "dev":
        _handle_dev_command(message_id, chat_id, sender_id, text, group)
    else:
        _handle_generic_agent_command(
            agent_cfg, message_id, chat_id, sender_id, text, group
        )


# ── Generic agent command handling ──


def _handle_generic_agent_command(
    agent_cfg, message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Handle commands for non-dev agents. Supports: done, clear, status, help, activate."""
    if not _check_agent_access(group, agent_cfg.name):
        reply_text(
            message_id, f"⚠️ 当前权限组 ({group}) 不支持 {agent_cfg.command} 模式。"
        )
        audit.log_denied(sender_id, chat_id, f"group={group} tried {agent_cfg.command}")
        return

    parts = text.strip().split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    arg_lower = arg.lower()

    if arg_lower == "done":
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            _cleanup_before_deactivate(chat_id)
            _agent_session_store.deactivate(chat_id)
            reply_text(
                message_id, f"已退出「{agent_cfg.display_name}」模式，回到普通聊天。"
            )
            audit.log_admin_action(sender_id, f"{agent_cfg.name}-done", chat_id)
        else:
            reply_text(message_id, f"当前未在「{agent_cfg.display_name}」模式中。")
        return

    if arg_lower == "clear":
        if _agent_session_store:
            _cleanup_before_deactivate(chat_id)
            _agent_session_store.deactivate(chat_id)
        reply_text(message_id, f"「{agent_cfg.display_name}」会话已清除。")
        audit.log_admin_action(sender_id, f"{agent_cfg.name}-clear", chat_id)
        return

    if arg_lower == "status":
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            state = _agent_session_store.get(chat_id)
            domain_info = (
                f"\n业务域：{'+'.join(state.domains)}"
                if state and state.domains
                else ""
            )
            req_info = (
                f"\n需求：{state.requirement}" if state and state.requirement else ""
            )
            reply_text(
                message_id,
                f"**{agent_cfg.display_name}：已激活**{domain_info}{req_info}",
            )
        else:
            reply_text(
                message_id,
                f"**{agent_cfg.display_name}：未激活**\n\n使用 `{agent_cfg.command}` 进入。",
            )
        return

    if arg_lower == "help":
        lines = [f"**{agent_cfg.display_name}** — {agent_cfg.description}\n"]
        lines.append(f"`{agent_cfg.command}` — 进入模式")
        if agent_cfg.requires_domain:
            lines.append(
                f"`{agent_cfg.command} <业务域> [业务域...]` — 指定业务域进入（支持多域）"
            )
        lines.append(f"`{agent_cfg.command} done` — 退出模式")
        lines.append(f"`{agent_cfg.command} clear` — 清除会话")
        lines.append(f"`{agent_cfg.command} status` — 查看状态")
        reply_text(message_id, "\n".join(lines))
        return

    # Activate the agent
    if agent_cfg.requires_domain:
        # Try to match domain name(s)
        available = agent.discover_domains()

        if not arg:
            if len(available) == 0:
                reply_text(message_id, "未发现业务域，请在 biz/ 目录下创建业务域。")
            elif len(available) == 1:
                _activate_generic_agent(
                    message_id, chat_id, sender_id, agent_cfg, [available[0]], "", group
                )
            else:
                _send_domain_select_card(chat_id, available, agent_name=agent_cfg.name)
            return

        if len(available) == 0:
            reply_text(message_id, "未发现业务域，请在 biz/ 目录下创建业务域。")
            return

        # Parse multi-domain: match all leading words that are valid domains
        words = arg.split()
        matched = [w for w in words if w in available]
        remainder = " ".join(w for w in words if w not in available)

        if matched:
            _activate_generic_agent(
                message_id, chat_id, sender_id, agent_cfg, matched, remainder, group
            )
        elif len(available) == 1:
            _activate_generic_agent(
                message_id, chat_id, sender_id, agent_cfg, [available[0]], arg, group
            )
        else:
            _send_domain_select_card(
                chat_id, available, requirement=arg, agent_name=agent_cfg.name
            )
            return
    else:
        # No domain needed — activate directly
        if _agent_session_store:
            _agent_session_store.activate(chat_id, "", arg, agent_name=agent_cfg.name)
        audit.log_admin_action(
            sender_id, f"{agent_cfg.name}-start", arg[:80] if arg else "(no args)"
        )
        _color = _AGENT_COLORS.get(agent_cfg.name, "blue")
        reply_header_card(
            message_id,
            f"发 `{agent_cfg.command} done` 退出。",
            agent_cfg.display_name,
            _color,
        )
        if arg:
            _handle_agent_message(
                agent_cfg.name, message_id, chat_id, sender_id, arg, group
            )


def _activate_generic_agent(
    message_id: str,
    chat_id: str,
    sender_id: str,
    agent_cfg,
    domains: list[str],
    requirement: str,
    group: str,
) -> None:
    """Activate a generic (non-dev) agent and optionally send the first message."""
    if _agent_session_store:
        _agent_session_store.activate(
            chat_id, domains, requirement, agent_name=agent_cfg.name
        )
    domain_display = "+".join(domains)
    audit.log_admin_action(
        sender_id,
        f"{agent_cfg.name}-start",
        f"{domain_display}: {requirement[:80] if requirement else '(explore)'}",
    )

    _color = _AGENT_COLORS.get(agent_cfg.name, "blue")
    if requirement:
        reply_header_card(
            message_id,
            (
                f"业务域：{domain_display}\n需求：{requirement}\n\n"
                f"后续消息将由「{agent_cfg.display_name}」处理。发 `{agent_cfg.command} done` 退出。"
            ),
            agent_cfg.display_name,
            _color,
        )
        _handle_agent_message(
            agent_cfg.name, message_id, chat_id, sender_id, requirement, group
        )
    else:
        reply_header_card(
            message_id,
            (
                f"业务域：{domain_display}\n\n"
                f"你可以直接提问。发 `{agent_cfg.command} done` 退出。"
            ),
            f"{agent_cfg.display_name} · Explore",
            _color,
        )


# ── /dev command handling ──


def _handle_dev_command(
    message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Handle /dev subcommands."""
    if not _check_agent_access(group, "dev"):
        reply_text(message_id, f"⚠️ 当前权限组 ({group}) 不支持 /dev 模式。")
        audit.log_denied(sender_id, chat_id, f"group={group} tried /dev")
        return

    parts = text.strip().split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    arg_lower = arg.lower()

    if not arg:
        # No argument: select domain interactively or auto-select
        domains = agent.discover_domains()
        if len(domains) == 0:
            reply_text(message_id, "未发现业务域，请在 biz/ 目录下创建业务域。")
        elif len(domains) == 1:
            _activate_dev(message_id, chat_id, sender_id, domains[0], "", group)
        else:
            _send_domain_select_card(chat_id, domains, agent_name="dev")
        return

    if arg_lower == "done":
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            _cleanup_before_deactivate(chat_id)
            _agent_session_store.deactivate(chat_id)
            reply_text(message_id, "已退出 dev 模式，回到普通聊天。")
            audit.log_admin_action(sender_id, "dev-done", chat_id)
        else:
            reply_text(message_id, "当前未在 dev 模式中。")
        return

    if arg_lower == "clear":
        if _agent_session_store:
            _cleanup_before_deactivate(chat_id)
            _agent_session_store.deactivate(chat_id)
        reply_text(message_id, "Dev 会话已清除。")
        audit.log_admin_action(sender_id, "dev-clear", chat_id)
        return

    if arg_lower == "status":
        if _agent_session_store and _agent_session_store.is_active(chat_id):
            state = _agent_session_store.get(chat_id)
            phase_label = _PHASE_DISPLAY.get(state.phase, state.phase) if state else ""
            lines = [f"**Dev 模式：已激活**"]
            lines.append(f"\n业务域：{'+'.join(state.domains)}")
            lines.append(f"阶段：{phase_label}")
            if state.requirement:
                lines.append(f"需求：{state.requirement}")
            if state.plan_summary:
                plan_preview = (
                    state.plan_summary[:200] + "..."
                    if len(state.plan_summary) > 200
                    else state.plan_summary
                )
                lines.append(f"方案：\n{plan_preview}")
            reply_text(message_id, "\n".join(lines))
        else:
            reply_text(
                message_id,
                "**Dev 模式：未激活**\n\n使用 `/dev <业务域> <需求描述>` 进入 dev 模式。",
            )
        return

    if arg_lower == "help":
        reply_text(message_id, _DEV_HELP_TEXT)
        return

    # /dev req <requirement> — set requirement and enter planning
    if arg_lower.startswith("req "):
        requirement = arg[4:].strip()
        if not requirement:
            reply_text(message_id, "请提供需求描述。用法：`/dev req <需求描述>`")
            return
        if not _agent_session_store or not _agent_session_store.is_active(chat_id):
            reply_text(
                message_id, "当前未在 dev 模式中。请先使用 `/dev <业务域>` 进入。"
            )
            return
        _agent_session_store.set_requirement(chat_id, requirement)
        _agent_session_store.set_phase(chat_id, "planning")
        reply_text(message_id, f"已设置需求，进入方案设计阶段。\n\n> {requirement}")
        _handle_dev_message(
            "",
            chat_id,
            sender_id,
            f"请分析以下需求并设计开发方案：\n{requirement}",
            group,
        )
        return

    # /dev go — advance to implementing
    if arg_lower == "go":
        if not _agent_session_store or not _agent_session_store.is_active(chat_id):
            reply_text(message_id, "当前未在 dev 模式中。")
            return
        state = _agent_session_store.get(chat_id)
        if state and state.phase == "implementing":
            reply_text(message_id, "当前已在开发执行阶段。")
            return
        _agent_session_store.set_phase(chat_id, "implementing")
        reply_text(message_id, "进入开发执行阶段。")
        _handle_dev_message(
            "", chat_id, sender_id, "[系统：用户确认开始开发，请按方案执行]", group
        )
        return

    # /dev plan — go back to planning (regression)
    if arg_lower == "plan":
        if not _agent_session_store or not _agent_session_store.is_active(chat_id):
            reply_text(message_id, "当前未在 dev 模式中。")
            return
        _agent_session_store.set_phase(chat_id, "planning")
        reply_text(message_id, "回到方案设计阶段。")
        _handle_dev_message(
            "",
            chat_id,
            sender_id,
            "[系统：用户要求回到方案设计，请重新审视当前方案]",
            group,
        )
        return

    # /dev explore — go back to explore (regression)
    if arg_lower == "explore":
        if not _agent_session_store or not _agent_session_store.is_active(chat_id):
            reply_text(message_id, "当前未在 dev 模式中。")
            return
        _agent_session_store.set_phase(chat_id, "explore")
        reply_text(message_id, "回到探索阶段。")
        return

    if arg_lower == "list":
        domains = agent.discover_domains()
        if domains:
            reply_text(
                message_id,
                "**可用业务域：**\n\n" + "\n".join(f"- {p}" for p in domains),
            )
        else:
            reply_text(message_id, "未发现业务域，请在 biz/ 目录下创建业务域。")
        return

    if arg_lower == "push":
        _handle_dev_push(message_id, chat_id)
        return

    # /dev <domain> [requirement] or /dev [requirement] — activate dev mode
    domains = agent.discover_domains()
    first_word = arg.split()[0] if arg else ""

    if first_word in domains:
        domain = first_word
        requirement = arg[len(first_word) :].strip()
    elif len(domains) == 1:
        domain = domains[0]
        requirement = arg
    elif len(domains) == 0:
        reply_text(message_id, "未发现业务域，请在 biz/ 目录下创建业务域。")
        return
    else:
        # Multiple domains, no domain specified — send selection card
        _send_domain_select_card(chat_id, domains, requirement=arg, agent_name="dev")
        return

    _activate_dev(message_id, chat_id, sender_id, domain, requirement, group)


def _activate_dev(
    message_id: str,
    chat_id: str,
    sender_id: str,
    domain: str,
    requirement: str,
    group: str,
) -> None:
    """Activate dev mode and optionally send the first message to the agent."""
    if _agent_session_store:
        state = _agent_session_store.activate(
            chat_id, domain, requirement, agent_name="dev"
        )
        # Set initial phase based on whether requirement is provided
        if requirement:
            state.phase = "planning"
        else:
            state.phase = "explore"
    audit.log_admin_action(
        sender_id,
        "dev-start",
        f"{domain}: {requirement[:80] if requirement else '(explore)'}",
    )

    _color = _AGENT_COLORS.get("dev", "blue")
    if requirement:
        reply_header_card(
            message_id,
            (
                f"业务域：{domain}\n需求：{requirement}\n\n"
                f"后续消息将由研发 Agent 处理。发 `/dev done` 退出。"
            ),
            "Dev · Planning",
            _color,
        )
        _handle_dev_message(
            message_id,
            chat_id,
            sender_id,
            f"请分析以下需求并设计开发方案：\n{requirement}",
            group,
        )
    else:
        reply_header_card(
            message_id,
            (
                f"业务域：{domain}\n\n"
                f"你可以直接提问了解项目代码和架构。需要开发时描述需求即可（如「帮我实现 XXX 功能」），Agent 会自动识别并进入方案设计。\n"
                f"发 `/dev done` 退出。"
            ),
            "Dev · Explore",
            _color,
        )


def _send_domain_select_card(
    chat_id: str, domains: list[str], requirement: str = "", agent_name: str = "dev"
) -> None:
    """Send an interactive card for domain selection."""
    buttons = []
    for p in domains:
        buttons.append(
            {
                "text": p,
                "value": {
                    "action": "select_domain",
                    "domain": p,
                    "requirement": requirement,
                    "agent_name": agent_name,
                },
                "type": "default",
            }
        )
    send_select_card(
        chat_id,
        "**请选择业务域：**",
        buttons,
        header_title="选择业务域",
        header_template="blue",
    )


def _handle_dev_message(
    message_id: str, chat_id: str, sender_id: str, text: str, group: str
) -> None:
    """Handle a message in dev mode — forward to dev agent."""
    budget_msg = agent.check_daily_budget(sender_id)
    if budget_msg:
        reply_text(message_id, budget_msg)
        return
    logger.info(
        "Dev mode message from chat=%s sender=%s: %s", chat_id, sender_id, text[:80]
    )

    # Build status suffix (mode + gap)
    _state = _agent_session_store.get(chat_id) if _agent_session_store else None
    current_phase = _state.phase if _state else "explore"
    _suffix = _build_status_suffix(chat_id, _state)

    # Snapshot branches before agent runs (for commit detection)
    pre_branches = _snapshot_branches(chat_id)

    if message_id:
        _init_status = _random_thinking(dev=True)
        card_msg_id = reply_card(message_id, " ", status_line=_init_status)
    else:
        _init_status = _random_thinking(dev=True)
        card_msg_id = send_card(chat_id, " ", status_line=_init_status)

    if card_msg_id:
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        last_status: str = _init_status
        _last_card_update: float = 0.0

        def _append_text(text_so_far: str) -> None:
            if len(text_so_far) <= _THINKING_MAX_LEN and not content_parts:
                thinking_parts.append(_format_thinking(text_so_far))
            elif len(text_so_far) <= _THINKING_MAX_LEN:
                content_parts.append(_format_thinking(text_so_far))
            else:
                content_parts.append(text_so_far)

        def _build_body() -> str:
            return _THINKING_SEP.join(content_parts) if content_parts else " "

        def on_progress(text_so_far: str, status_line: str, is_done: bool) -> None:
            nonlocal card_msg_id, last_status, _last_card_update

            # Throttle card updates: min 1s interval (except is_done which always fires)
            now = time.monotonic()
            if not is_done and (now - _last_card_update) < 1.0:
                if text_so_far and not status_line:
                    _append_text(text_so_far)
                elif status_line:
                    last_status = status_line
                return

            if is_done and status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line
            elif text_so_far and not status_line:
                _append_text(text_so_far)
                body = _build_body()
                if len(body) > _MAX_CARD_CHARS and len(content_parts) > 1:
                    carry = 1
                    if len(content_parts) > 2 and len(content_parts[-2]) < 200:
                        carry = 2
                    old_body = _THINKING_SEP.join(content_parts[:-carry])
                    update_card(
                        card_msg_id,
                        old_body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    content_parts[:] = content_parts[-carry:]
                    thinking_parts.clear()
                    body = _build_body()
                    new_id = send_card(
                        chat_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    if new_id:
                        card_msg_id = new_id
                    else:
                        update_card(
                            card_msg_id,
                            body,
                            status_line=last_status,
                            thinking_lines=thinking_parts,
                        )
                else:
                    update_card(
                        card_msg_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
            elif status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line

            _last_card_update = time.monotonic()

        def _send_final(text: str) -> None:
            if message_id:
                reply_text(message_id, text)
            else:
                send_message(chat_id, text)

        try:
            response = agent.run_dev(
                chat_id,
                text,
                on_progress=on_progress,
                sender_id=sender_id,
                group=group,
                status_suffix=_suffix,
            )
            if response != "（已停止）":
                if thinking_parts or content_parts:
                    _send_final(response)
                else:
                    update_card(card_msg_id, response, status_line=last_status)
                _check_and_send_push_card(chat_id, pre_branches)
                _check_phase_reminder(chat_id, current_phase)
        except TimeoutError:
            update_card(card_msg_id, "⚠️ 该会话正忙，请稍后再试。")
        except Exception as e:
            logger.exception("Dev agent error for chat=%s", chat_id)
            update_card(card_msg_id, _error_message(e))
    else:
        try:
            response = agent.run_dev(
                chat_id, text, sender_id=sender_id, group=group, status_suffix=_suffix
            )
            if response != "（已停止）":
                if message_id:
                    reply_text(message_id, response)
                else:
                    send_message(chat_id, response)
                _check_and_send_push_card(chat_id, pre_branches)
                _check_phase_reminder(chat_id, current_phase)
        except Exception as e:
            logger.exception("Dev agent error for chat=%s", chat_id)
            if message_id:
                reply_text(message_id, _error_message(e))
            else:
                send_message(chat_id, _error_message(e))


# ── Generic agent message handling ──


def _handle_agent_message(
    agent_name: str,
    message_id: str,
    chat_id: str,
    sender_id: str,
    text: str,
    group: str,
) -> None:
    """Handle a message in a generic (non-dev) agent session — forward to agent."""
    budget_msg = agent.check_daily_budget(sender_id)
    if budget_msg:
        reply_text(message_id, budget_msg)
        return
    logger.info(
        "Agent[%s] message from chat=%s sender=%s: %s",
        agent_name,
        chat_id,
        sender_id,
        text[:80],
    )

    # Build status suffix (mode + gap)
    _state = _agent_session_store.get(chat_id) if _agent_session_store else None
    _suffix = _build_status_suffix(chat_id, _state)

    if message_id:
        _init_status = _random_thinking(dev=True)
        card_msg_id = reply_card(message_id, " ", status_line=_init_status)
    else:
        _init_status = _random_thinking(dev=True)
        card_msg_id = send_card(chat_id, " ", status_line=_init_status)

    if card_msg_id:
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        last_status: str = _init_status
        _last_card_update: float = 0.0

        def _append_text(text_so_far: str) -> None:
            if len(text_so_far) <= _THINKING_MAX_LEN and not content_parts:
                thinking_parts.append(_format_thinking(text_so_far))
            elif len(text_so_far) <= _THINKING_MAX_LEN:
                content_parts.append(_format_thinking(text_so_far))
            else:
                content_parts.append(text_so_far)

        def _build_body() -> str:
            return _THINKING_SEP.join(content_parts) if content_parts else " "

        def on_progress(text_so_far: str, status_line: str, is_done: bool) -> None:
            nonlocal card_msg_id, last_status, _last_card_update

            now = time.monotonic()
            if not is_done and (now - _last_card_update) < 1.0:
                if text_so_far and not status_line:
                    _append_text(text_so_far)
                elif status_line:
                    last_status = status_line
                return

            if is_done and status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line
            elif text_so_far and not status_line:
                _append_text(text_so_far)
                body = _build_body()
                if len(body) > _MAX_CARD_CHARS and len(content_parts) > 1:
                    carry = 1
                    if len(content_parts) > 2 and len(content_parts[-2]) < 200:
                        carry = 2
                    old_body = _THINKING_SEP.join(content_parts[:-carry])
                    update_card(
                        card_msg_id,
                        old_body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    content_parts[:] = content_parts[-carry:]
                    thinking_parts.clear()
                    body = _build_body()
                    new_id = send_card(
                        chat_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
                    if new_id:
                        card_msg_id = new_id
                    else:
                        update_card(
                            card_msg_id,
                            body,
                            status_line=last_status,
                            thinking_lines=thinking_parts,
                        )
                else:
                    update_card(
                        card_msg_id,
                        body,
                        status_line=last_status,
                        thinking_lines=thinking_parts,
                    )
            elif status_line:
                update_card(
                    card_msg_id,
                    _build_body(),
                    status_line=status_line,
                    thinking_lines=thinking_parts,
                )
                last_status = status_line

            _last_card_update = time.monotonic()

        def _send_final(text: str) -> None:
            if message_id:
                reply_text(message_id, text)
            else:
                send_message(chat_id, text)

        try:
            response = agent.run_agent_session(
                chat_id,
                text,
                agent_name,
                on_progress=on_progress,
                sender_id=sender_id,
                group=group,
                status_suffix=_suffix,
            )
            if response != "（已停止）":
                if thinking_parts or content_parts:
                    _send_final(response)
                else:
                    update_card(card_msg_id, response, status_line=last_status)
        except TimeoutError:
            update_card(card_msg_id, "⚠️ 该会话正忙，请稍后再试。")
        except Exception as e:
            logger.exception("Agent[%s] error for chat=%s", agent_name, chat_id)
            update_card(card_msg_id, _error_message(e))
    else:
        try:
            response = agent.run_agent_session(
                chat_id,
                text,
                agent_name,
                sender_id=sender_id,
                group=group,
                status_suffix=_suffix,
            )
            if response != "（已停止）":
                if message_id:
                    reply_text(message_id, response)
                else:
                    send_message(chat_id, response)
        except Exception as e:
            logger.exception("Agent[%s] error for chat=%s", agent_name, chat_id)
            if message_id:
                reply_text(message_id, _error_message(e))
            else:
                send_message(chat_id, _error_message(e))


# ── Worktree cleanup helper ──


def _cleanup_before_deactivate(chat_id: str) -> None:
    """Clean up worktrees before deactivating an agent session.

    If any repo has uncommitted changes, the worktree is preserved
    and the user is notified. It will be reused if the user re-enters
    dev mode, or eventually cleaned by periodic cleanup (which also
    skips dirty worktrees).
    """
    if not _agent_session_store:
        return
    state = _agent_session_store.get(chat_id)
    if not state or not state.active:
        return
    from agents import get_agent

    cfg = get_agent(state.agent_name)
    if not cfg or not getattr(cfg, "needs_isolation", False):
        return
    if not state.domains:
        return
    try:
        from core.worktree import remove_worktrees, check_dirty_state

        for domain in state.domains:
            dirty = check_dirty_state(domain, chat_id)
            if dirty:
                repos_list = ", ".join(d["repo"] for d in dirty)
                send_message(
                    chat_id,
                    f"⚠️ 以下仓库有未提交的修改，worktree 已保留：{repos_list}\n重新进入 dev 模式可继续操作。",
                )
                logger.warning(
                    "Skipped worktree cleanup (dirty): domain=%s chat=%s repos=%s",
                    domain,
                    chat_id,
                    repos_list,
                )
                continue
            remove_worktrees(domain, chat_id)
            logger.info("Cleaned worktrees: domain=%s chat=%s", domain, chat_id)
    except Exception:
        logger.exception("Error cleaning worktrees for chat=%s", chat_id)


def _get_effective_repos_path(chat_id: str, state) -> str:
    """Return the worktree path if active, otherwise the standard repos path."""
    if not state or not state.domain:
        return ""
    from agents import get_agent

    cfg = get_agent(state.agent_name)
    if cfg and getattr(cfg, "needs_isolation", False) and len(state.domains) == 1:
        from core.worktree import get_worktree_path

        wt_path = get_worktree_path(state.domain, chat_id)
        if wt_path:
            return wt_path
    from core.biz import repos_path

    return repos_path(state.domain)


# ── Post-commit push card ──


def _snapshot_branches(chat_id: str) -> dict[str, tuple[str, str]]:
    """Take a snapshot of repo branches before agent runs."""
    if not _agent_session_store or not _agent_session_store.is_active(chat_id):
        return {}
    state = _agent_session_store.get(chat_id)
    if not state:
        return {}
    base_dir = _get_effective_repos_path(chat_id, state)
    return agent.snapshot_repo_branches(state.domain, base_dir=base_dir)


def _handle_dev_push(message_id: str, chat_id: str) -> None:
    """Handle /dev push — manually trigger push card for all feature branches."""
    if not _agent_session_store or not _agent_session_store.is_active(chat_id):
        reply_text(
            message_id, "当前未在 dev 模式中。使用 `/dev <业务域> <需求>` 进入。"
        )
        return
    state = _agent_session_store.get(chat_id)
    if not state:
        reply_text(message_id, "当前未在 dev 模式中。")
        return
    base_dir = _get_effective_repos_path(chat_id, state)
    repos = agent.find_feature_repos(state.domain, base_dir=base_dir)
    if repos:
        _send_push_card(chat_id, state.domain, repos)
    else:
        reply_text(message_id, "未发现 feature 分支，所有仓库都在 main/master 上。")


def _check_and_send_push_card(
    chat_id: str, pre_branches: dict[str, tuple[str, str]]
) -> None:
    """After dev agent returns, check if any repo switched to a new feature branch."""
    if not _agent_session_store or not _agent_session_store.is_active(chat_id):
        return
    state = _agent_session_store.get(chat_id)
    if not state:
        return
    try:
        base_dir = _get_effective_repos_path(chat_id, state)
        repos = agent.find_changed_repos(state.domain, pre_branches, base_dir=base_dir)
        if repos:
            _send_push_card(chat_id, state.domain, repos)
    except Exception:
        logger.exception("Error checking pushable repos for chat=%s", chat_id)


def _send_push_card(chat_id: str, domain: str, repos: list[dict]) -> None:
    """Send an interactive card with Push & PR button."""
    lines = []
    for r in repos:
        lines.append(f"- `{r['repo']}` 分支 `{r['branch']}`")

    markdown_text = "\n".join(lines)
    # Encode the specific repos to push in the button value
    repos_brief = [{"repo": r["repo"], "branch": r["branch"]} for r in repos]
    button_value = {"action": "push_and_pr", "domain": domain, "repos": repos_brief}

    send_action_card(
        chat_id,
        markdown_text,
        "Push & 创建 PR",
        button_value,
        header_title="代码推送",
        header_template="green",
    )


# ── Card action handling ──


def _make_toast_response(toast_type: str, content: str) -> P2CardActionTriggerResponse:
    """Build a P2CardActionTriggerResponse with a toast (lark_oapi uses dict-based init)."""
    return P2CardActionTriggerResponse(
        {"toast": {"type": toast_type, "content": content}}
    )


def _handle_select_domain(
    value: dict,
    operator_id: str,
    chat_id: str,
    card_msg_id: str,
) -> P2CardActionTriggerResponse | None:
    """Handle domain selection button click."""
    agent_name = value.get("agent_name", "dev")
    op_group = _permissions.get_group(operator_id, chat_id) if _permissions else "admin"
    if not _check_agent_access(op_group, agent_name):
        return _make_toast_response("error", f"权限组 ({op_group}) 无权操作")

    domain = value.get("domain", "")
    requirement = value.get("requirement", "")
    agent_name = value.get("agent_name", "dev")
    if not domain:
        return _make_toast_response("error", "缺少业务域信息")

    # Update the selection card to show the chosen domain
    update_card(card_msg_id, f"已选择业务域：**{domain}**", is_done=True)

    # Activate agent in a background thread (may trigger agent if requirement provided)
    threading.Thread(
        target=_activate_agent_from_card,
        args=(chat_id, operator_id, domain, requirement, agent_name),
        daemon=True,
    ).start()

    mode = "dev" if requirement else "explore"
    return _make_toast_response("info", f"{domain} · {mode}")


def _activate_agent_from_card(
    chat_id: str,
    operator_id: str,
    domain: str,
    requirement: str,
    agent_name: str = "dev",
) -> None:
    """Background thread: activate agent mode after domain selection card click."""
    try:
        if _agent_session_store:
            _agent_session_store.activate(
                chat_id, domain, requirement, agent_name=agent_name
            )
        audit.log_admin_action(
            operator_id,
            f"{agent_name}-start",
            f"{domain}: {requirement[:80] if requirement else '(explore)'}",
        )

        group = (
            _permissions.get_group(operator_id, chat_id) if _permissions else "admin"
        )

        _color = _AGENT_COLORS.get(agent_name, "blue")

        if agent_name == "dev":
            # Dev agent: set phase based on requirement
            state = _agent_session_store.get(chat_id) if _agent_session_store else None
            if state:
                state.phase = "planning" if requirement else "explore"

            if requirement:
                send_header_card(
                    chat_id,
                    (
                        f"业务域：{domain}\n需求：{requirement}\n\n"
                        f"后续消息将由研发 Agent 处理。发 `/dev done` 退出。"
                    ),
                    "Dev · Planning",
                    _color,
                )
                _handle_dev_message(
                    "",
                    chat_id,
                    operator_id,
                    f"请分析以下需求并设计开发方案：\n{requirement}",
                    group,
                )
            else:
                send_header_card(
                    chat_id,
                    (
                        f"业务域：{domain}\n\n"
                        f"你可以直接提问了解项目代码和架构。需要开发时描述需求即可（如「帮我实现 XXX 功能」），Agent 会自动识别并进入方案设计。\n"
                        f"发 `/dev done` 退出。"
                    ),
                    "Dev · Explore",
                    _color,
                )
        else:
            # Generic agent activation
            from agents import get_agent

            cfg = get_agent(agent_name)
            display = cfg.display_name if cfg else agent_name
            cmd = cfg.command if cfg else f"/{agent_name}"

            if requirement:
                send_header_card(
                    chat_id,
                    (
                        f"业务域：{domain}\n需求：{requirement}\n\n"
                        f"后续消息将由「{display}」处理。发 `{cmd} done` 退出。"
                    ),
                    display,
                    _color,
                )
                _handle_agent_message(
                    agent_name, "", chat_id, operator_id, requirement, group
                )
            else:
                send_header_card(
                    chat_id,
                    (f"业务域：{domain}\n\n" f"你可以直接提问。发 `{cmd} done` 退出。"),
                    f"{display} · Explore",
                    _color,
                )
    except Exception:
        logger.exception(
            "Error activating agent from card for chat=%s agent=%s", chat_id, agent_name
        )


def _handle_phase_reminder_advance(
    value: dict,
    operator_id: str,
    chat_id: str,
    card_msg_id: str,
) -> P2CardActionTriggerResponse | None:
    """Handle phase reminder 'advance' button click."""
    op_group = _permissions.get_group(operator_id, chat_id) if _permissions else "admin"
    if not _check_agent_access(op_group, "dev"):
        return _make_toast_response("error", f"权限组 ({op_group}) 无权操作")

    target_phase = value.get("target_phase", "")
    if not target_phase:
        return _make_toast_response("error", "缺少目标阶段")

    if _agent_session_store:
        _agent_session_store.set_phase(chat_id, target_phase)
    update_card(card_msg_id, f"已切换到 {target_phase} 阶段。", is_done=True)

    # Send kickoff message in background
    kickoff_messages = {
        "planning": "[系统：用户确认进入方案设计。请总结需求并设计开发方案。]",
        "implementing": "[系统：用户确认开始开发，请按方案执行。]",
    }
    msg = kickoff_messages.get(target_phase, "")
    if msg:

        def _bg():
            try:
                group = (
                    _permissions.get_group(operator_id, chat_id)
                    if _permissions
                    else "admin"
                )
                _handle_dev_message("", chat_id, operator_id, msg, group)
            except Exception:
                logger.exception("Error in phase advance kickoff for chat=%s", chat_id)

        threading.Thread(target=_bg, daemon=True).start()

    return _make_toast_response("info", f"进入 {target_phase}")


def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse | None:
    """Handle a card button click event."""
    try:
        value = data.event.action.value or {}
        operator_id = data.event.operator.open_id or ""
        chat_id = data.event.context.open_chat_id or ""
        card_msg_id = data.event.context.open_message_id or ""
    except Exception:
        logger.exception("Failed to parse card action event")
        return None

    action = value.get("action", "")

    if action == "select_domain":
        return _handle_select_domain(value, operator_id, chat_id, card_msg_id)

    if action == "show_help":
        send_raw_card(chat_id, _build_help_card_json())
        return _make_toast_response("info", "已发送帮助信息")

    if action == "show_agent_help":
        agent_cmd = value.get("agent_cmd", "")
        help_text = _get_agent_help_text(agent_cmd)
        if help_text:
            send_message(chat_id, help_text)
        else:
            from agents import find_by_command

            cfg = find_by_command(agent_cmd)
            if cfg:
                send_message(
                    chat_id,
                    f"**{cfg.display_name}** (`{cfg.command}`)\n\n{cfg.description}\n\n使用 `{cfg.command} help` 查看详细帮助。",
                )
            else:
                send_message(chat_id, f"未找到 `{agent_cmd}` 的帮助信息。")
        return _make_toast_response("info", "已发送帮助信息")

    if action == "phase_reminder_advance":
        return _handle_phase_reminder_advance(value, operator_id, chat_id, card_msg_id)

    if action == "phase_reminder_continue":
        update_card(card_msg_id, "继续当前阶段。", is_done=True)
        return _make_toast_response("info", "继续")

    if action != "push_and_pr":
        return None

    # Permission check — only admin can push
    if _permissions and not _permissions.is_admin(operator_id):
        logger.info("Non-admin %s tried push_and_pr", operator_id)
        return _make_toast_response("error", "仅管理员可推送代码")

    domain = value.get("domain", "")
    repos_brief = value.get("repos", [])
    if not domain or not repos_brief:
        return _make_toast_response("error", "缺少业务域信息")

    # Update card to show processing (remove button)
    update_card(card_msg_id, "⏳ 正在推送代码...", is_done=True)

    # Run the actual work in a background thread
    threading.Thread(
        target=_do_push_and_pr,
        args=(chat_id, domain, repos_brief, operator_id, card_msg_id),
        daemon=True,
    ).start()

    return _make_toast_response("info", "正在推送代码...")


def _do_push_and_pr(
    chat_id: str,
    domain: str,
    repos_brief: list[dict],
    operator_id: str,
    card_msg_id: str,
) -> None:
    """Background thread: push code and create PR, then update the card."""
    try:
        # Reconstruct full repo info — use worktree path when active
        state = _agent_session_store.get(chat_id) if _agent_session_store else None
        repos_dir = (
            _get_effective_repos_path(chat_id, state)
            if state
            else agent._biz_repos_path(domain)
        )
        repos = []
        for rb in repos_brief:
            repos.append(
                {
                    "path": str(Path(repos_dir) / rb["repo"]),
                    "repo": rb["repo"],
                    "branch": rb["branch"],
                }
            )
        if not repos:
            update_card(card_msg_id, "没有找到可推送的 feature 分支。", is_done=True)
            return

        results = []
        for repo_info in repos:
            result = agent.push_and_create_pr(repo_info)
            results.append(result)

        # Build result message
        lines = []
        has_error = False
        for r in results:
            if r["pr_url"] and not r["error"]:
                # MR was actually created
                lines.append(
                    f"✅ **{r['repo']}** (`{r['branch']}`)\nMR 已创建：[查看 MR]({r['pr_url']})"
                )
            elif r["pr_url"] and r["error"]:
                # Push succeeded but MR not auto-created, provide link
                lines.append(
                    f"**{r['repo']}** (`{r['branch']}`)\n{r['error']}\n👉 [点击创建 MR]({r['pr_url']})"
                )
                has_error = True
            elif r["error"]:
                lines.append(f"**{r['repo']}** (`{r['branch']}`)\n⚠️ {r['error']}")
                has_error = True
            else:
                lines.append(f"**{r['repo']}** (`{r['branch']}`)\n已推送")

        summary = "\n\n".join(lines)
        update_card(card_msg_id, summary, is_done=True)

        audit.log_admin_action(
            operator_id,
            "push-and-pr",
            f"domain={domain}, repos={len(results)}, errors={'yes' if has_error else 'no'}",
        )
    except Exception:
        logger.exception("Error in _do_push_and_pr for domain=%s", domain)
        update_card(card_msg_id, "⚠️ 推送过程中发生错误，请手动操作。", is_done=True)


# ── Phase turn reminder ──

_IMPLEMENTING_REMINDER_INTERVAL = 8


def _check_phase_reminder(chat_id: str, phase: str) -> None:
    """Increment phase round count and send reminder card at interval thresholds.

    Explore reminders suppressed when no requirement set (pure Q&A).
    Agent handles phase transitions via update_dev_phase tool; reminders are backup.
    """
    if not _agent_session_store:
        return
    if phase not in ("explore", "planning", "implementing"):
        return

    turns = _agent_session_store.increment_phase_rounds(chat_id)

    # Suppress explore reminders when user hasn't expressed dev intent.
    # Agent naturally mentions /dev req when it detects intent; reminders
    # at round 10 serve as a one-time fallback for long sessions.
    if phase == "explore":
        state = _agent_session_store.get(chat_id)
        if not state or not state.requirement:
            if turns == 10:
                # One-time hint for long explore sessions
                send_message(
                    chat_id,
                    "💡 如果需要进入开发工作流，可以发 `/dev req` 或 `/dev req <需求>`。",
                )
            return

    intervals = {
        "explore": _EXPLORE_REMINDER_INTERVAL,
        "planning": _PLANNING_REMINDER_INTERVAL,
        "implementing": _IMPLEMENTING_REMINDER_INTERVAL,
    }
    interval = intervals.get(phase, 5)
    if turns > 0 and turns % interval == 0:
        _send_phase_reminder_card(chat_id, phase, turns)


def _send_phase_reminder_card(chat_id: str, phase: str, turns: int) -> None:
    """Send a phase progress reminder card with action buttons."""
    if phase == "explore":
        markdown_text = f"已探索 **{turns}** 轮"
        buttons = [
            {
                "text": "进入方案设计",
                "value": {
                    "action": "phase_reminder_advance",
                    "target_phase": "planning",
                },
                "type": "primary",
            },
            {
                "text": "继续探索",
                "value": {"action": "phase_reminder_continue"},
                "type": "default",
            },
        ]
        header_title = "🔄 探索进度提醒"
    elif phase == "planning":
        markdown_text = f"方案设计已进行 **{turns}** 轮"
        buttons = [
            {
                "text": "开始开发",
                "value": {
                    "action": "phase_reminder_advance",
                    "target_phase": "implementing",
                },
                "type": "primary",
            },
            {
                "text": "继续设计",
                "value": {"action": "phase_reminder_continue"},
                "type": "default",
            },
        ]
        header_title = "🔄 方案设计进度提醒"
    elif phase == "implementing":
        markdown_text = f"开发已进行 **{turns}** 轮"
        buttons = [
            {
                "text": "回到方案设计",
                "value": {
                    "action": "phase_reminder_advance",
                    "target_phase": "planning",
                },
                "type": "default",
            },
            {
                "text": "继续开发",
                "value": {"action": "phase_reminder_continue"},
                "type": "default",
            },
        ]
        header_title = "🔄 开发进度提醒"
    else:
        return

    send_select_card(
        chat_id,
        markdown_text,
        buttons,
        header_title=header_title,
        header_template="indigo",
    )


# ── Helpers ──


def _extract_post_text(content: dict) -> str:
    """Extract plain text from a Feishu 'post' (rich text) message."""
    lines: list[str] = []
    # post structure: {"zh_cn": {"title": "...", "content": [[{tag, text}, ...], ...]}}
    # or without locale key: {"title": "...", "content": [[...]]}
    post = content
    # Try to unwrap locale key (zh_cn, en_us, etc.)
    if "content" not in post:
        for v in post.values():
            if isinstance(v, dict) and "content" in v:
                post = v
                break

    title = post.get("title", "")
    if title:
        lines.append(title)

    for paragraph in post.get("content", []):
        parts: list[str] = []
        for element in paragraph:
            tag = element.get("tag", "")
            if tag == "text":
                parts.append(element.get("text", ""))
            elif tag == "a":
                parts.append(element.get("text", "") or element.get("href", ""))
            elif tag == "at":
                # Keep @mention text but it'll be cleaned later
                parts.append(element.get("text", ""))
            elif tag == "img":
                parts.append("[图片]")
        lines.append("".join(parts))

    return "\n".join(lines).strip()


def _extract_media(
    message_type: str, content: dict, message_id: str = ""
) -> tuple[list[str], list[str]]:
    """Extract and download images/files from a message.

    Returns (image_paths, file_paths) with absolute paths of downloaded files.
    """
    from core.media import download_image, download_file

    image_paths: list[str] = []
    file_paths: list[str] = []

    if message_type == "image":
        image_key = content.get("image_key", "")
        if image_key and message_id:
            path = download_image(message_id, image_key)
            if path:
                image_paths.append(path)

    elif message_type == "file":
        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "")
        if file_key and message_id:
            path = download_file(message_id, file_key, file_name)
            if path:
                file_paths.append(path)

    elif message_type == "post":
        # Extract images from rich text paragraphs
        post = content
        if "content" not in post:
            for v in post.values():
                if isinstance(v, dict) and "content" in v:
                    post = v
                    break
        for paragraph in post.get("content", []):
            for element in paragraph:
                if element.get("tag") == "img":
                    image_key = element.get("image_key", "")
                    if image_key and message_id:
                        path = download_image(message_id, image_key)
                        if path:
                            image_paths.append(path)

    return image_paths, file_paths


def _compose_prompt_with_media(
    text: str, image_paths: list[str], file_paths: list[str]
) -> str:
    """Compose a prompt that includes text and media file references."""
    parts: list[str] = []

    if text:
        parts.append(text)

    for path in image_paths:
        parts.append(f"\n[用户发送了一张图片，路径: {path}]")

    for path in file_paths:
        parts.append(f"\n[用户发送了一个文件，路径: {path}]")

    if image_paths and file_paths:
        parts.append("\n请使用 Read 工具查看上述图片和文件内容，然后回复用户。")
    elif image_paths:
        parts.append("\n请使用 Read 工具查看图片内容，然后回复用户。")
    elif file_paths:
        parts.append("\n请使用 Read 工具查看文件内容，然后回复用户。")

    return "".join(parts).strip()


def _error_message(e: Exception) -> str:
    """Convert an exception to a user-friendly error message."""
    err_str = str(e).lower()
    if "429" in err_str or "rate_limit" in err_str or "rate limit" in err_str:
        return "⚠️ API 限流中，请稍后再试。"
    if "timeout" in err_str:
        return "⚠️ 处理超时，请简化请求后重试。"
    if "connection" in err_str:
        return "⚠️ 服务连接异常，请稍后重试。"
    return "⚠️ 处理出错，请重试。如持续出错，使用 /clear 清除会话。"


def _build_help_text() -> str:
    """Build help text dynamically from shared data + registered agents."""
    from agents import list_agents

    lines = [
        "我是飞书 AI 助手，基于 Claude Agent SDK 构建。",
        "",
        "可用功能：",
    ]
    for cap in agent.CAPABILITIES:
        lines.append(f"- {cap}")

    lines.extend(["", "命令："])
    for cmd, desc in agent.COMMANDS:
        lines.append(f"- {cmd} — {desc}")

    agents = list_agents()
    for a in agents:
        lines.append(f"- {a.command} help — {a.display_name}（管理员）")

    lines.extend(["", "在群聊中 @我 即可交互，私聊直接发消息。"])

    return "\n".join(lines)


# ── Interactive card builders ──


def _build_welcome_card_json() -> str:
    """Build a welcome card for empty @mention (first contact)."""
    from agents import list_agents

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": "直接向我提问即可开始对话，也可以用下方按钮了解更多。",
        },
    ]

    # Build agent quick-intro lines
    agents = list_agents()
    if agents:
        agent_lines = " · ".join(f"`{a.command}` {a.display_name}" for a in agents)
        elements.append(
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": f"专业模式：{agent_lines}",
            }
        )

    elements.append({"tag": "hr"})

    # Buttons
    buttons: list[dict] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看完整帮助"},
            "type": "primary",
            "behaviors": [{"type": "callback", "value": {"action": "show_help"}}],
        },
    ]
    for a in agents:
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": a.display_name},
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {"action": "show_agent_help", "agent_cmd": a.command},
                    }
                ],
            }
        )

    columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]
    elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})

    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "你好！我是 AI 助手"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
    return json.dumps(card)


def _build_help_card_json() -> str:
    """Build an interactive help card for /help command."""
    from agents import list_agents

    elements: list[dict] = []

    # Basic capabilities (from shared data)
    cap_lines = "\n".join(f"- {c}" for c in agent.CAPABILITIES)
    elements.append(
        {
            "tag": "markdown",
            "content": f"**基础能力**\n{cap_lines}",
        }
    )

    # Agent modes
    agents = list_agents()
    if agents:
        elements.append({"tag": "hr"})
        agent_lines = "\n".join(
            f"- **{a.display_name}** `{a.command}` — {a.description}" for a in agents
        )
        elements.append(
            {
                "tag": "markdown",
                "content": f"**Agent 模式**（管理员）\n{agent_lines}",
            }
        )

    # Commands (from shared data)
    cmd_lines = "\n".join(f"- `{cmd}` — {desc}" for cmd, desc in agent.COMMANDS)
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "markdown",
            "content": f"**通用命令**\n{cmd_lines}",
        }
    )

    elements.append(
        {
            "tag": "markdown",
            "text_size": "notation",
            "content": "在群聊中 @我 即可交互，私聊直接发消息。",
        }
    )

    # Agent help buttons
    if agents:
        elements.append({"tag": "hr"})
        buttons = []
        for a in agents:
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"{a.display_name}帮助"},
                    "type": "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "action": "show_agent_help",
                                "agent_cmd": a.command,
                            },
                        }
                    ],
                }
            )
        columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]
        elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})

    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "飞书 AI 助手"},
            "subtitle": {"tag": "plain_text", "content": "功能与命令"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
    return json.dumps(card)


# Map agent commands to their help text constants
_AGENT_HELP_TEXTS: dict[str, str] = {}  # populated lazily


def _get_agent_help_text(agent_cmd: str) -> str | None:
    """Get help text for an agent command. Returns None if not found."""
    # Lazy-populate from known help texts
    if not _AGENT_HELP_TEXTS:
        _AGENT_HELP_TEXTS["/dev"] = _DEV_HELP_TEXT
    return _AGENT_HELP_TEXTS.get(agent_cmd)


_DEV_HELP_TEXT = """\
**Dev 模式** — 研发助手

**进入 dev 模式：**
/dev — 多业务域时弹出选择卡片
/dev <业务域> — 进入探索模式（自由提问，了解代码）
/dev <业务域> <需求描述> — 带需求直接进入方案设计
/dev list — 列出可用业务域

**模式内操作：**
/dev req <需求> — 手动设置需求，进入方案设计
/dev go — 跳过方案审批，直接进入开发执行
/dev push — 推送 feature 分支并创建 MR/PR
/dev status — 查看当前状态（含阶段、需求、方案）
/dev done — 退出 dev 模式
/dev clear — 清除会话历史

**分阶段工作流：**
1. **探索** → 自由提问，了解项目代码和架构
2. 提出需求后弹出确认卡片 → 点击确认进入 **方案设计**
3. Agent 输出方案 → 弹出审批卡片 → 点击"开始开发"进入 **开发执行**
4. Agent 写代码、测试、commit（不 push）
5. commit 后自动弹出推送按钮，也可用 /dev push 手动触发
6. /dev done 退出，回到普通聊天

**快捷方式：**
- 探索中直接说"go"或"开始开发" → 跳过方案设计
- 方案设计中说"go"或"开始开发" → 跳过审批卡片

需要对应权限组才能使用。"""

_ADMIN_HELP_TEXT = """\
**管理员命令**

**权限组管理：**
/admin list — 查看权限分配
/admin group @某某 developer — 设置用户权限组
/admin group @某某 remove — 移除用户权限
/admin group list — 查看分组详情
/admin chat-group developer — 设置当前群默认组
/admin chat-group remove — 移除群默认组

**角色管理：**
/admin role @某某 角色描述 — 设置群成员角色
/admin role list — 查看当前群角色列表
/admin role remove @某某 — 移除成员角色

**运维工具：**
/admin stats — 用量总览（今日汇总 + 按 agent 分组 + 7 天趋势）
/admin log [N] — 最近 N 条请求明细（默认 20）
/admin status — 服务状态（运行时长、MCP、会话、内存）
/admin sessions — 会话管理（列出活跃会话）
/admin sessions clear <chat_id> — 清除指定会话

**测试：**
/sudo <组名> — 模拟权限组（仅当前会话）
/sudo off — 恢复 admin 权限

说明：
- admin 组拥有全部权限
- developer 组可使用 Chat/Ask/Dev，工具受路径限制
- member 组可使用 Chat/Ask，仅只读
- 权限组定义在 config.yaml 的 permission_groups 段"""
