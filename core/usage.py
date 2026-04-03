"""Usage logging — SQLite-backed per-request cost & token tracking.

Reuses the shared connection pattern from persistent_session.py.
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── RunResult — structured return from run_agent_core ──

@dataclass
class RunResult:
    """Structured result from a single agent run."""
    text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    sdk_turns: int = 0
    tool_count: int = 0
    duration_s: float = 0.0
    stop_reason: str = ""        # "end_turn" / "max_turns" / "cancelled" / "error"
    input_tokens: int = 0
    output_tokens: int = 0
    usage: dict | None = None    # raw usage dict from SDK


# ── SQLite schema ──

_CREATE_USAGE_TABLE = """\
CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    chat_id         TEXT    NOT NULL DEFAULT '',
    sender_id       TEXT    NOT NULL DEFAULT '',
    agent_name      TEXT    NOT NULL DEFAULT '',
    domain          TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    stop_reason     TEXT    NOT NULL DEFAULT '',
    session_id      TEXT    DEFAULT NULL,
    sdk_turns       INTEGER NOT NULL DEFAULT 0,
    tool_count      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    duration_s      REAL    NOT NULL DEFAULT 0.0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    usage_json      TEXT    NOT NULL DEFAULT '{}'
)"""

_CREATE_USAGE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_chat_ts ON usage_log(chat_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_sender_ts ON usage_log(sender_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_agent_ts ON usage_log(agent_name, timestamp)",
]


# ── UsageStore ──

class UsageStore:
    """Append-only usage log backed by SQLite."""

    def __init__(self, db_path: str = "./data/sessions.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._get_connection()
            conn.execute(_CREATE_USAGE_TABLE)
            for idx in _CREATE_USAGE_INDEXES:
                conn.execute(idx)
            conn.commit()

    def log(
        self,
        *,
        chat_id: str = "",
        sender_id: str = "",
        agent_name: str = "",
        domain: str = "",
        model: str = "",
        result: RunResult | None = None,
    ) -> None:
        """Write one usage row. Pass the RunResult from the agent run."""
        r = result or RunResult()
        usage_json = json.dumps(r.usage, ensure_ascii=False) if r.usage else "{}"
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO usage_log "
                "(timestamp, chat_id, sender_id, agent_name, domain, model, "
                " stop_reason, session_id, sdk_turns, tool_count, "
                " cost_usd, duration_s, input_tokens, output_tokens, usage_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    chat_id,
                    sender_id,
                    agent_name,
                    domain,
                    model,
                    r.stop_reason,
                    r.session_id,
                    r.sdk_turns,
                    r.tool_count,
                    r.cost_usd,
                    r.duration_s,
                    r.input_tokens,
                    r.output_tokens,
                    usage_json,
                ),
            )
            conn.commit()

    def query_daily(self, sender_id: str) -> float:
        """Return today's cumulative cost_usd for a sender (UTC day)."""
        import datetime
        today_start = datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        with self._lock:
            conn = self._get_connection()
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                "WHERE sender_id = ? AND timestamp >= ?",
                (sender_id, today_start),
            ).fetchone()
            return row[0] if row else 0.0

    # ── Admin query methods ──

    def query_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent usage log entries."""
        with self._lock:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, chat_id, sender_id, agent_name, cost_usd, "
                "sdk_turns, tool_count, duration_s, input_tokens, output_tokens "
                "FROM usage_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.row_factory = None
            return [dict(r) for r in rows]

    def query_daily_summary(self) -> dict:
        """Return today's aggregate: request count, total cost, turns, tokens."""
        import datetime
        today_start = datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        with self._lock:
            conn = self._get_connection()
            row = conn.execute(
                "SELECT COUNT(*) AS cnt, "
                "COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(sdk_turns), 0) AS turns, "
                "COALESCE(SUM(input_tokens), 0) AS inp, "
                "COALESCE(SUM(output_tokens), 0) AS outp "
                "FROM usage_log WHERE timestamp >= ?",
                (today_start,),
            ).fetchone()
            return {
                "count": row[0],
                "cost_usd": row[1],
                "turns": row[2],
                "input_tokens": row[3],
                "output_tokens": row[4],
            }

    def query_by_agent(self, days: int = 1) -> list[dict]:
        """Aggregate usage grouped by agent_name for the last N days."""
        import datetime
        start = datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        if days > 1:
            start = start.replace(day=start.day)  # just use timedelta
            import datetime as _dt
            start = start - _dt.timedelta(days=days - 1)
        ts = start.timestamp()
        with self._lock:
            conn = self._get_connection()
            rows = conn.execute(
                "SELECT agent_name, COUNT(*) AS cnt, "
                "COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(sdk_turns), 0) AS turns, "
                "COALESCE(SUM(input_tokens), 0) AS inp, "
                "COALESCE(SUM(output_tokens), 0) AS outp "
                "FROM usage_log WHERE timestamp >= ? "
                "GROUP BY agent_name ORDER BY cost DESC",
                (ts,),
            ).fetchall()
            return [
                {"agent": r[0] or "chat", "count": r[1], "cost_usd": r[2],
                 "turns": r[3], "input_tokens": r[4], "output_tokens": r[5]}
                for r in rows
            ]

    def query_by_day(self, days: int = 7) -> list[dict]:
        """Aggregate usage grouped by date for the last N days."""
        import datetime
        start = datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0,
        ) - datetime.timedelta(days=days - 1)
        ts = start.timestamp()
        with self._lock:
            conn = self._get_connection()
            rows = conn.execute(
                "SELECT date(timestamp, 'unixepoch') AS day, "
                "COUNT(*) AS cnt, "
                "COALESCE(SUM(cost_usd), 0) AS cost "
                "FROM usage_log WHERE timestamp >= ? "
                "GROUP BY day ORDER BY day DESC",
                (ts,),
            ).fetchall()
            return [{"date": r[0], "count": r[1], "cost_usd": r[2]} for r in rows]
