"""SQLite WAL flight recorder: `.flywire/flight_log.db` (spec §6, contract §5)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    workspace_root TEXT NOT NULL,
    mode TEXT NOT NULL,
    last_code_edit_ts REAL DEFAULT 0.0,
    last_test_pass_ts REAL DEFAULT 0.0,
    last_test_pass_count INTEGER DEFAULT 0,
    consecutive_hard_blocks INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    step_idx INTEGER NOT NULL,
    ts REAL NOT NULL,
    tool_name TEXT NOT NULL,
    op_class TEXT NOT NULL,
    target_path TEXT,
    state_hash TEXT,
    verdict TEXT NOT NULL,        -- ALLOW | STEER | HARD_BLOCK
    rule_id TEXT,
    reason TEXT,
    exit_code INTEGER,
    error_hash TEXT,
    latency_ms REAL NOT NULL,
    args_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_step ON tool_events(session_id, step_idx);
CREATE INDEX IF NOT EXISTS idx_events_session_path ON tool_events(session_id, target_path);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open with the contract §0 PRAGMAs. Shared with tape files."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def close_checkpointed(conn: sqlite3.Connection) -> None:
    """Fold the WAL back so no -wal/-shm sidecar survives (portable single file)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


@dataclass(frozen=True)
class SessionState:
    last_code_edit_ts: float
    last_test_pass_ts: float
    last_test_pass_count: int


@dataclass(frozen=True)
class WriteRow:
    target_path: str
    state_hash: str | None
    ts: float


@dataclass(frozen=True)
class EventRow:
    session_id: str
    step_idx: int
    ts: float
    tool_name: str
    op_class: str
    target_path: str | None
    verdict: str
    rule_id: str | None
    reason: str | None
    latency_ms: float


class FlightDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(path)
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        close_checkpointed(self.conn)

    def start_session(self, session_id: str, root: str, mode: str, ts: float = 0.0) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (session_id, started_at, workspace_root, mode) "
                "VALUES (?, ?, ?, ?)",
                (session_id, ts, root, mode),
            )

    def insert_event(
        self,
        *,
        session_id: str,
        step_idx: int,
        ts: float,
        tool_name: str,
        op_class: str,
        target_path: str | None,
        state_hash: str | None,
        verdict: str,
        rule_id: str | None,
        reason: str | None,
        latency_ms: float,
        args_json: str,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tool_events (session_id, step_idx, ts, tool_name, op_class, "
                "target_path, state_hash, verdict, rule_id, reason, latency_ms, args_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    step_idx,
                    ts,
                    tool_name,
                    op_class,
                    target_path,
                    state_hash,
                    verdict,
                    rule_id,
                    reason,
                    latency_ms,
                    args_json,
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def set_result(self, event_id: int, *, exit_code: int, error_hash: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tool_events SET exit_code = ?, error_hash = ? WHERE id = ?",
                (exit_code, error_hash, event_id),
            )

    def session(self, session_id: str) -> SessionState:
        row = self.conn.execute(
            "SELECT last_code_edit_ts, last_test_pass_ts, last_test_pass_count "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return SessionState(*row)

    def set_code_edit(self, session_id: str, ts: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_code_edit_ts = ? WHERE session_id = ?", (ts, session_id)
            )

    def set_test_pass(self, session_id: str, ts: float, passed: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_test_pass_ts = ?, last_test_pass_count = ? "
                "WHERE session_id = ?",
                (ts, passed, session_id),
            )

    def set_hard_blocks(self, session_id: str, count: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET consecutive_hard_blocks = ? WHERE session_id = ?",
                (count, session_id),
            )

    def recent_writes(self, session_id: str, limit: int, *, audit: bool) -> list[WriteRow]:
        """Last `limit` applied FILE_WRITEs, newest first (audit mode applies everything)."""
        rows = self.conn.execute(
            "SELECT target_path, state_hash, ts FROM tool_events "
            "WHERE session_id = ? AND op_class = 'FILE_WRITE' AND (verdict = 'ALLOW' OR ?) "
            "ORDER BY step_idx DESC LIMIT ?",
            (session_id, audit, limit),
        ).fetchall()
        return [WriteRow(*r) for r in rows]

    def recent_executions(self, session_id: str, limit: int) -> list[tuple[int, int, str | None]]:
        """(step_idx, exit_code, error_hash) of the last executed calls, newest first."""
        rows = self.conn.execute(
            "SELECT step_idx, exit_code, error_hash FROM tool_events "
            "WHERE session_id = ? AND exit_code IS NOT NULL ORDER BY step_idx DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def rule_fired_since(self, session_id: str, rule_id: str, step_idx: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM tool_events WHERE session_id = ? AND step_idx > ? "
                "AND rule_id = ? AND verdict != 'ALLOW' LIMIT 1",
                (session_id, step_idx, rule_id),
            ).fetchone()
            is not None
        )

    def intervention_since(self, session_id: str, ts: float) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM tool_events WHERE session_id = ? AND ts > ? "
                "AND verdict != 'ALLOW' LIMIT 1",
                (session_id, ts),
            ).fetchone()
            is not None
        )

    def recent_events(self, limit: int, *, interventions_only: bool) -> list[EventRow]:
        """Newest-first events across sessions, for `flywire status` / `flywire log`."""
        where = "WHERE verdict != 'ALLOW' " if interventions_only else ""
        rows = self.conn.execute(
            "SELECT session_id, step_idx, ts, tool_name, op_class, target_path, verdict, "
            f"rule_id, reason, latency_ms FROM tool_events {where}ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [EventRow(*r) for r in rows]
