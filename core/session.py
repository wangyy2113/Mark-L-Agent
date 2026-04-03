"""Session store interface — chat_id → session_id mapping with TTL.

Inspired by Claude Code's state management pattern:
- Single source of truth for session state
- SQLite-backed persistence (see persistent_session.py)
- Summary storage for context compression
- Thread-safe with per-store locking

This module defines the interface. PersistentSessionStore in
persistent_session.py is the production implementation.
"""

import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    """Interface for chat session storage."""

    def get(self, chat_id: str) -> str | None:
        """Get session_id for a chat. Returns None if expired or not found."""
        ...

    def set(self, chat_id: str, session_id: str) -> None:
        """Store session_id for a chat."""
        ...

    def delete(self, chat_id: str) -> None:
        """Clear session_id (but preserve summary)."""
        ...

    def get_summary(self, chat_id: str) -> str | None:
        """Get compressed context summary."""
        ...

    def set_summary(self, chat_id: str, summary: str) -> None:
        """Store compressed context summary."""
        ...

    def clear_all(self, chat_id: str) -> None:
        """Clear both session_id and summary (for /clear command)."""
        ...
