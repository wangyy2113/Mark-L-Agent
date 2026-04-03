"""Shared SQLite connection manager.

All state modules (sessions, usage, etc.) share a single WAL-mode
connection to data/sessions.db. This avoids multiple connections
to the same file and ensures consistent WAL behavior.

Usage:
    from core.db import get_connection

    conn = get_connection()
    conn.execute("SELECT ...")
"""

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_db_path: str = ""


def init(db_path: str = "./data/sessions.db") -> sqlite3.Connection:
    """Initialize the shared database connection. Call once at startup."""
    global _conn, _db_path
    _db_path = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(db_path, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            logger.info("Database initialized: %s", db_path)
    return _conn


def get_connection() -> sqlite3.Connection:
    """Get the shared database connection. Must call init() first."""
    if _conn is None:
        raise RuntimeError("Database not initialized. Call core.db.init() first.")
    return _conn


def get_lock() -> threading.Lock:
    """Get the shared database lock for transaction safety."""
    return _lock
