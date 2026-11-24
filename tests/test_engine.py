from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from conftest import write, write_call

from flywire.engine import EnvelopeEngine
from flywire.guards.scope_guard import lines_deleted

Factory = Callable[..., EnvelopeEngine]


def events(engine: EnvelopeEngine) -> list[tuple[str, str, str | None, str | None]]:
    conn = sqlite3.connect(engine.root / ".flywire/flight_log.db")
    try:
        return conn.execute(
            "SELECT op_class, verdict, rule_id, target_path FROM tool_events ORDER BY step_idx"
        ).fetchall()
    finally:
        conn.close()


def test_read_only_is_allowed_and_never_logged(make_engine: Factory) -> None:
    engine = make_engine()
    verdict = engine.evaluate("read_file", {"path": "../../etc/passwd"})
    assert verdict.action == "ALLOW"
    assert events(engine) == []


def test_allow_is_logged_with_args_and_latency(make_engine: Factory) -> None:
    engine = make_engine()
    verdict = engine.evaluate("write_file", {"path": "src/ok.py", "content": "x = 1\n"})
    assert (verdict.action, verdict.rule_id, verdict.engine) == ("ALLOW", None, "envelope")
    assert verdict.latency_ms > 0
    assert events(engine) == [("FILE_WRITE", "ALLOW", None, "src/ok.py")]
    conn = sqlite3.connect(engine.root / ".flywire/flight_log.db")
    args = json.loads(conn.execute("SELECT args_json FROM tool_events").fetchone()[0])
    conn.close()
    assert args == {"path": "src/ok.py", "content": "x = 1\n"}


def test_traversal_is_hard_block(make_engine: Factory) -> None:
    engine = make_engine()
    verdict = engine.evaluate("write_file", {"path": "../evil.py", "content": "x"})
    assert (verdict.action, verdict.rule_id, verdict.engine) == (
        "HARD_BLOCK",
        "scope-path-traversal",
        "scope_guard",
    )
    assert events(engine) == [("FILE_WRITE", "HARD_BLOCK", "scope-path-traversal", "../evil.py")]


def test_protected_paths(make_engine: Factory) -> None:
    engine = make_engine()
    for path in [".github/workflows/ci.yml", ".env", ".env.local", "uv.lock", "migrations/1.sql"]:
        verdict = engine.evaluate("write_file", {"path": path, "content": "x"})
        assert (verdict.action, verdict.rule_id) == ("STEER", "scope-protected-path"), path
    assert engine.evaluate("write_file", {"path": "src/env.py", "content": "x"}).action == "ALLOW"


def test_lines_deleted_counts_removed_lines_not_moves() -> None:
    assert lines_deleted(b"a\nb\nc\n", b"c\na\nb\n") == 0
    assert lines_deleted(b"a\nb\nc\n", b"a\n") == 2
    assert lines_deleted(b"", b"x\n") == 0


def test_mass_deletion(make_engine: Factory) -> None:
    engine = make_engine("[scope]\nmax_lines_deleted_per_turn = 3\n")
    write(engine.root, "src/big.py", "".join(f"x{i} = {i}\n" for i in range(10)))
    ok = engine.evaluate("edit", {"path": "src/big.py", "old_str": "x0 = 0\nx1 = 1\n", "new_str": ""})
    assert ok.action == "ALLOW"
    verdict = engine.evaluate("write_file", {"path": "src/big.py", "content": "x0 = 0\n"})
    assert (verdict.action, verdict.rule_id) == ("STEER", "scope-mass-deletion")
    assert "9" in verdict.reason


def test_blast_radius_counts_distinct_applied_files(make_engine: Factory) -> None:
    engine = make_engine("[scope]\nmax_files_modified = 2\n")
    assert write_call(engine, "a.py", "1\n").verdict.action == "ALLOW"
    assert write_call(engine, ".env", "S=1\n").verdict.action == "STEER"  # blocked: not counted
    assert write_call(engine, "b.py", "1\n").verdict.action == "ALLOW"
    assert write_call(engine, "a.py", "2\n").verdict.action == "ALLOW"  # already modified
    third = write_call(engine, "c.py", "1\n").verdict
    assert (third.action, third.rule_id) == ("STEER", "scope-blast-radius")


def test_audit_mode_downgrades_but_logs_raw_verdict(make_engine: Factory) -> None:
    engine = make_engine("[envelope]\nmode = 'audit'\n")
    verdict = engine.evaluate("write_file", {"path": ".env", "content": "x"})
    assert (verdict.action, verdict.rule_id) == ("ALLOW", "scope-protected-path")
    assert events(engine)[-1][1] == "STEER"


def test_strict_mode_upgrades_and_counts_consecutive_hard_blocks(make_engine: Factory) -> None:
    engine = make_engine("[envelope]\nmode = 'strict'\n")
    assert engine.evaluate("write_file", {"path": ".env", "content": "x"}).action == "HARD_BLOCK"
    assert engine.evaluate("bash", {"command": "git reset --hard"}).action == "HARD_BLOCK"
    assert engine.consecutive_hard_blocks == 2
    assert engine.evaluate("bash", {"command": "ls"}).action == "ALLOW"
    assert engine.consecutive_hard_blocks == 0
    assert [e[1] for e in events(engine)] == ["HARD_BLOCK", "HARD_BLOCK", "ALLOW"]


def test_steer_does_not_reset_hard_block_counter(make_engine: Factory) -> None:
    engine = make_engine()
    engine.evaluate("bash", {"command": "git reset --hard"})
    engine.evaluate("write_file", {"path": ".env", "content": "x"})  # STEER
    assert engine.consecutive_hard_blocks == 1


def test_engine_rule_can_be_disabled(make_engine: Factory) -> None:
    engine = make_engine("[envelope]\ndisabled_rules = ['scope-protected-path']\n")
    assert engine.evaluate("write_file", {"path": ".env", "content": "x"}).action == "ALLOW"


def test_session_row_created(make_engine: Factory) -> None:
    engine = make_engine("[envelope]\nmode = 'strict'\n")
    conn = sqlite3.connect(engine.root / ".flywire/flight_log.db")
    row = conn.execute("SELECT session_id, workspace_root, mode FROM sessions").fetchone()
    conn.close()
    assert row == (engine.session_id, str(engine.root), "strict")
    assert len(engine.session_id) == 32


def test_feedback_text_for_ast_violation(make_engine: Factory, tmp_path: Path) -> None:
    engine = make_engine()
    write(tmp_path, "src/a.py", "x = 1\n")
    verdict = engine.evaluate("write_file", {"path": "src/a.py", "content": "x = 1  # noqa\n"})
    assert verdict.feedback() == (
        "[FLYWIRE: py-no-lint-suppression] A new lint/type suppression comment "
        "(`# noqa` / `# type: ignore`) was added.\n"
        "Action required: Fix the underlying lint or type error instead of suppressing it."
    )
