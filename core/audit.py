"""Audit logging — writes all requests, tool calls, and admin actions to audit.log."""

import logging
from logging.handlers import TimedRotatingFileHandler

_audit_logger: logging.Logger | None = None


def init(log_path: str = "audit.log") -> None:
    """Initialize the audit logger with daily rotation (30-day retention)."""
    global _audit_logger
    if _audit_logger is not None:
        return  # Already initialized

    _audit_logger = logging.getLogger("audit")
    _audit_logger.setLevel(logging.INFO)
    _audit_logger.propagate = False  # Don't bubble up to root logger

    handler = TimedRotatingFileHandler(
        log_path, when="D", backupCount=30, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _audit_logger.addHandler(handler)


def _log(message: str) -> None:
    if _audit_logger:
        _audit_logger.info(message)


def log_request(sender_id: str, chat_id: str, text: str) -> None:
    """Record an incoming user request."""
    preview = text[:200].replace("\n", " ")
    _log(f"REQUEST | sender={sender_id} | chat={chat_id} | text={preview}")


def log_tool_call(sender_id: str, chat_id: str, tool_name: str, detail: str = "") -> None:
    """Record a tool invocation."""
    detail_part = f" | detail={detail}" if detail else ""
    _log(f"TOOL | sender={sender_id} | chat={chat_id} | tool={tool_name}{detail_part}")


def log_admin_action(sender_id: str, action: str, detail: str) -> None:
    """Record an admin command execution."""
    _log(f"ADMIN | sender={sender_id} | action={action} | detail={detail}")


def log_denied(sender_id: str, chat_id: str, reason: str) -> None:
    """Record a denied request."""
    _log(f"DENIED | sender={sender_id} | chat={chat_id} | reason={reason}")
