"""SQLite-backed session stores for chat and agent sessions.

Drop-in replacements for SessionStore and AgentSessionStore.
Both share one SQLite table with a JSON state column for extensibility.
"""

import json
import logging
import sqlite3
import threading
import time

from core.agent_session import AgentSessionStore, AgentState
from core.session import SessionStore

logger = logging.getLogger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS sessions (
    chat_id    TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'chat',
    session_id TEXT,
    active     INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (chat_id, type)
)"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at)"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.commit()
    return conn


# ── Shared connection per db_path ──
_connections: dict[str, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


def _get_connection(db_path: str) -> sqlite3.Connection:
    with _conn_lock:
        if db_path not in _connections:
            _connections[db_path] = _connect(db_path)
        return _connections[db_path]


# ── Chat session store ──

class PersistentSessionStore(SessionStore):
    """SQLite-backed SessionStore. Interface-compatible with the in-memory version."""

    def __init__(self, ttl: int = 86400, db_path: str = "./data/sessions.db"):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._db = _get_connection(db_path)
        self._type = "chat"

    def get(self, chat_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, updated_at FROM sessions WHERE chat_id = ? AND type = ?",
                (chat_id, self._type),
            ).fetchone()
            if row is None:
                return None
            session_id, updated_at = row
            if time.time() - updated_at > self._ttl:
                # Soft expire: don't delete, just return None
                return None
            return session_id

    def set(self, chat_id: str, session_id: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (chat_id, type, session_id, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id, type) DO UPDATE SET session_id = ?, updated_at = ?",
                (chat_id, self._type, session_id, time.time(), session_id, time.time()),
            )
            self._db.commit()

    def delete(self, chat_id: str) -> None:
        """Clear session_id but preserve state (summary etc.)."""
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET session_id = NULL, updated_at = ? "
                "WHERE chat_id = ? AND type = ?",
                (time.time(), chat_id, self._type),
            )
            self._db.commit()

    def get_updated_at(self, chat_id: str) -> float | None:
        """Return the updated_at timestamp for gap calculation."""
        with self._lock:
            row = self._db.execute(
                "SELECT updated_at FROM sessions WHERE chat_id = ? AND type = ?",
                (chat_id, self._type),
            ).fetchone()
            return row[0] if row else None

    def cleanup(self) -> int:
        """Remove entries older than 30 days."""
        cutoff = time.time() - 30 * 86400
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE type = ? AND updated_at < ?",
                (self._type, cutoff),
            )
            self._db.commit()
            return cursor.rowcount

    # ── Summary storage (for context compression) ────────────────────────────

    def get_summary(self, chat_id: str) -> str | None:
        """Return compressed context summary from the state JSON column."""
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM sessions WHERE chat_id = ? AND type = ?",
                (chat_id, self._type),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row[0]) if row[0] else {}
            return data.get("summary")

    def set_summary(self, chat_id: str, summary: str) -> None:
        """Store compressed context summary in the state JSON column."""
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM sessions WHERE chat_id = ? AND type = ?",
                (chat_id, self._type),
            ).fetchone()
            if row:
                data = json.loads(row[0]) if row[0] else {}
                data["summary"] = summary
                self._db.execute(
                    "UPDATE sessions SET state = ?, updated_at = ? "
                    "WHERE chat_id = ? AND type = ?",
                    (json.dumps(data, ensure_ascii=False), now, chat_id, self._type),
                )
            else:
                data = {"summary": summary}
                self._db.execute(
                    "INSERT INTO sessions (chat_id, type, session_id, state, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?)",
                    (chat_id, self._type, json.dumps(data, ensure_ascii=False), now),
                )
            self._db.commit()

    def clear_all(self, chat_id: str) -> None:
        """Clear both session_id and summary (for /clear command)."""
        with self._lock:
            self._db.execute(
                "DELETE FROM sessions WHERE chat_id = ? AND type = ?",
                (chat_id, self._type),
            )
            self._db.commit()

    def count_active(self) -> int:
        """Return number of chat sessions that have not expired (within TTL)."""
        cutoff = time.time() - self._ttl
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE type = ? AND updated_at >= ?",
                (self._type, cutoff),
            ).fetchone()
            return row[0] if row else 0


# ── Agent session store ──

class PersistentAgentSessionStore(AgentSessionStore):
    """SQLite-backed AgentSessionStore. Interface-compatible with the in-memory version."""

    def __init__(self, ttl: int = 86400, db_path: str = "./data/sessions.db"):
        self._lock = threading.Lock()
        self._db = _get_connection(db_path)
        self._ttl = ttl

    def _state_to_json(self, state: AgentState) -> str:
        """Serialize agent-specific fields to JSON."""
        return json.dumps({
            "domains": state.domains,
            "requirement": state.requirement,
            "phase": state.phase,
            "plan_summary": state.plan_summary,
            "phase_rounds": state.phase_rounds,
        }, ensure_ascii=False)

    def _row_to_state(self, row: tuple) -> AgentState:
        """Deserialize a DB row to AgentState.

        Row: (session_id, active, state_json, updated_at, type)
        """
        session_id, active, state_json, updated_at, agent_name = row
        data = json.loads(state_json) if state_json else {}
        # Backward-compatible: accept old "domain" string or new "domains" list
        domains = data.get("domains") or ([data["domain"]] if data.get("domain") else [])
        return AgentState(
            active=bool(active),
            agent_name=agent_name,
            domains=domains,
            requirement=data.get("requirement", ""),
            phase=data.get("phase", "explore"),
            plan_summary=data.get("plan_summary", ""),
            phase_rounds=data.get("phase_rounds", data.get("phase_turns", 0)),
            started_at=updated_at,
            session_id=session_id,
        )

    def activate(self, chat_id: str, domain: str | list[str], requirement: str, agent_name: str = "dev") -> AgentState:
        domains = domain if isinstance(domain, list) else [domain] if domain else []
        state = AgentState(active=True, agent_name=agent_name, domains=domains, requirement=requirement)
        state_json = self._state_to_json(state)
        now = time.time()
        with self._lock:
            # Deactivate any existing agent session for this chat first
            self._db.execute(
                "UPDATE sessions SET active = 0 "
                "WHERE chat_id = ? AND type != 'chat' AND type != ? AND active = 1",
                (chat_id, agent_name),
            )
            self._db.execute(
                "INSERT INTO sessions (chat_id, type, session_id, active, state, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(chat_id, type) DO UPDATE SET "
                "session_id = NULL, active = 1, state = ?, updated_at = ?",
                (chat_id, agent_name, None, state_json, now, state_json, now),
            )
            self._db.commit()
        return state

    def get(self, chat_id: str) -> AgentState | None:
        with self._lock:
            # Find active agent session for this chat (any type except 'chat')
            row = self._db.execute(
                "SELECT session_id, active, state, updated_at, type "
                "FROM sessions WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_state(row)

    def is_active(self, chat_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM sessions WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            return row is not None

    def list_active(self) -> list[dict]:
        """Return all active agent sessions (chat_id, type, state, updated_at)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT chat_id, type, state, updated_at "
                "FROM sessions WHERE type != 'chat' AND active = 1 "
                "ORDER BY updated_at DESC",
            ).fetchall()
            result = []
            for chat_id, agent_type, state_json, updated_at in rows:
                data = json.loads(state_json) if state_json else {}
                domains = data.get("domains") or ([data["domain"]] if data.get("domain") else [])
                result.append({
                    "chat_id": chat_id,
                    "type": agent_type,
                    "domains": domains,
                    "state": data.get("phase", ""),
                    "updated_at": updated_at,
                })
            return result

    def set_session_id(self, chat_id: str, session_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET session_id = ?, updated_at = ? "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (session_id, time.time(), chat_id),
            )
            self._db.commit()

    def clear_session_id(self, chat_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET session_id = NULL "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            )
            self._db.commit()

    def _update_state_field(self, chat_id: str, **updates) -> None:
        """Update specific fields in the serialized state JSON."""
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM sessions "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            if row is None:
                return
            data = json.loads(row[0]) if row[0] else {}
            data.update(updates)
            state_json = json.dumps(data, ensure_ascii=False)
            self._db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (state_json, time.time(), chat_id),
            )
            self._db.commit()

    def set_phase(self, chat_id: str, phase: str) -> None:
        self._update_state_field(chat_id, phase=phase, phase_rounds=0)

    def set_requirement(self, chat_id: str, requirement: str) -> None:
        self._update_state_field(chat_id, requirement=requirement)

    def set_plan(self, chat_id: str, plan_summary: str) -> None:
        self._update_state_field(chat_id, plan_summary=plan_summary)

    def transition_to_implementing(self, chat_id: str) -> None:
        """Transition to implementing phase. Session is preserved (no context loss)."""
        self._update_state_field(chat_id, phase="implementing", phase_rounds=0)

    def increment_phase_rounds(self, chat_id: str) -> int:
        """Increment and return user message round count for current phase (atomic read-modify-write)."""
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM sessions "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            if row is None:
                return 0
            data = json.loads(row[0]) if row[0] else {}
            turns = data.get("phase_rounds", data.get("phase_turns", 0)) + 1
            data["phase_rounds"] = turns
            data.pop("phase_turns", None)
            state_json = json.dumps(data, ensure_ascii=False)
            self._db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (state_json, time.time(), chat_id),
            )
            self._db.commit()
            return turns

    def deactivate(self, chat_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET active = 0 "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            )
            self._db.commit()

    def get_updated_at(self, chat_id: str) -> float | None:
        """Return the updated_at timestamp for gap calculation."""
        with self._lock:
            row = self._db.execute(
                "SELECT updated_at FROM sessions "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            return row[0] if row else None

    def get_session_id_with_ttl(self, chat_id: str) -> tuple[str | None, bool]:
        """Return (session_id, is_expired) for the active agent session.

        Unlike get() which always returns the state, this checks TTL on the session_id.
        Returns (session_id, False) if valid, (None, True) if expired, (None, False) if no session.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, updated_at FROM sessions "
                "WHERE chat_id = ? AND type != 'chat' AND active = 1",
                (chat_id,),
            ).fetchone()
            if row is None:
                return None, False
            session_id, updated_at = row
            if session_id is None:
                return None, False
            if time.time() - updated_at > self._ttl:
                return None, True  # expired
            return session_id, False
