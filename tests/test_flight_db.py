from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flywire.store.flight_db import FlightDB, connect


def test_connection_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_schema_exact_and_clean_close(tmp_path: Path) -> None:
    path = tmp_path / ".flywire" / "flight_log.db"
    db = FlightDB(path)
    db.start_session("s1", str(tmp_path), "steer")
    event_id = db.insert_event(
        session_id="s1",
        step_idx=0,
        ts=1.0,
        tool_name="write_file",
        op_class="FILE_WRITE",
        target_path="src/a.py",
        state_hash="h",
        verdict="STEER",
        rule_id="py-x",
        reason="r",
        latency_ms=0.5,
        args_json="{}",
    )
    db.set_result(event_id, exit_code=1, error_hash="e")
    assert (tmp_path / ".flywire/flight_log.db-wal").exists()
    db.close()
    assert not (tmp_path / ".flywire/flight_log.db-wal").exists()
    assert not (tmp_path / ".flywire/flight_log.db-shm").exists()

    raw = sqlite3.connect(path)
    names = {
        r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    }
    assert names == {
        "sessions",
        "tool_events",
        "idx_events_session_step",
        "idx_events_session_path",
    }
    cols = [r[1] for r in raw.execute("PRAGMA table_info(tool_events)")]
    assert cols == [
        "id", "session_id", "step_idx", "ts", "tool_name", "op_class", "target_path",
        "state_hash", "verdict", "rule_id", "reason", "exit_code", "error_hash",
        "latency_ms", "args_json",
    ]  # fmt: skip
    session_cols = [r[1] for r in raw.execute("PRAGMA table_info(sessions)")]
    assert session_cols == [
        "session_id", "started_at", "workspace_root", "mode", "last_code_edit_ts",
        "last_test_pass_ts", "last_test_pass_count", "consecutive_hard_blocks",
    ]  # fmt: skip
    row = raw.execute("SELECT verdict, rule_id, exit_code, error_hash FROM tool_events").fetchone()
    assert row == ("STEER", "py-x", 1, "e")
    raw.close()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    db = FlightDB(tmp_path / "f.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_event(
                session_id="missing",
                step_idx=0,
                ts=1.0,
                tool_name="t",
                op_class="SHELL_EXEC",
                target_path=None,
                state_hash=None,
                verdict="ALLOW",
                rule_id=None,
                reason=None,
                latency_ms=0.1,
                args_json="{}",
            )
    finally:
        db.close()
