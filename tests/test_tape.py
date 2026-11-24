from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import git, write
from test_stdio_proxy import AUTH_TEST, Wrapped

from flywire.store.tape import TAPE_SCHEMA, proposed_diff, redact, snapshot_tree

TAPE_ENV = "[envelope]\nemit_tape = true\n"


def tape_rows(path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def only_tape(root: Path) -> Path:
    tapes = list((root / ".flywire/tapes").glob("*.tape"))
    assert len(tapes) == 1, tapes
    return tapes[0]


def test_snapshot_is_index_isolated_and_excludes_flywire(repo: Path) -> None:
    index_before = (repo / ".git/index").read_bytes()
    write(repo, "src/a.py", "x = 1\n")
    write(repo, ".flywire/flight_log.db", "binary")
    tree = snapshot_tree(repo)
    assert len(tree) == 40
    assert (repo / ".git/index").read_bytes() == index_before
    listing = git(repo, "ls-tree", "-r", "--name-only", tree).split()
    assert listing == ["README.md", "src/a.py"]
    assert git(repo, "status", "--porcelain").split() == ["??", ".flywire/", "??", "src/"]


def test_proposed_diff_and_redaction() -> None:
    diff = proposed_diff("src/a.py", b"a\nb\n", b"a\nc")
    assert diff.startswith("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n")
    assert "-b\n+c\n\\ No newline at end of file\n" in diff
    assert "--- /dev/null\n+++ b/n.py" in proposed_diff("n.py", b"", b"x\n")
    assert proposed_diff("n.py", b"x\n", b"x\n") == ""
    text = "key sk-ant-" + "a" * 24 + " and Bearer abcdefghijklmnopqrstu and AKIA" + "B" * 16
    assert redact(text) == "key <REDACTED_SECRET> and <REDACTED_SECRET> and <REDACTED_SECRET>"


def test_wrap_emits_steered_and_allowed_steps(repo: Path) -> None:
    write(repo, "tests/test_auth.py", AUTH_TEST)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tests")
    write(repo, "envelope.toml", TAPE_ENV)
    head = git(repo, "rev-parse", "HEAD").strip()
    index_before = (repo / ".git/index").read_bytes()

    w = Wrapped(repo)
    bad = AUTH_TEST.replace("    assert b()\n", "")
    steered = w.call(1, "write_file", {"path": "tests/test_auth.py", "content": bad})
    feedback = steered["result"]["content"][0]["text"]
    ok = w.call(
        2, "write_file", {"path": "src/ok.py", "content": "token = 'sk-" + "z" * 24 + "'\n"}
    )
    assert "result" in ok
    failing = w.call(3, "run_command", {"command": "exit 4"})
    assert failing["result"]["structuredContent"]["exit_code"] == 4
    assert w.close()[0] == 0

    tape = only_tape(repo)
    assert not Path(f"{tape}-wal").exists() and not Path(f"{tape}-shm").exists()
    assert (repo / ".git/index").read_bytes() == index_before
    meta = tape_rows(
        tape,
        "SELECT schema_version, command_argv, repo_root, base_commit_sha, "
        "parent_tape_id, metadata_json FROM tape_meta",
    )
    assert len(meta) == 1
    version, argv, root, base, parent, metadata = meta[0]
    assert (version, json.loads(argv), root, base, parent) == (
        "0.1",
        [sys.executable, str(Path(__file__).parent / "mock_mcp_server.py")],
        str(repo.resolve()),
        head,
        None,
    )
    assert json.loads(metadata)["producer"] == "flywire"
    assert len(json.loads(metadata)["session_id"]) == 32

    steps = tape_rows(
        tape,
        "SELECT s.step_idx, s.kind, s.status, s.pre_tree_sha, s.post_tree_sha, s.diff_text, "
        "t.tool_name, t.arguments_json, t.result_text, t.is_error, t.exit_code "
        "FROM steps s JOIN tool_calls t USING (step_idx) ORDER BY s.step_idx",
    )
    assert [s[:3] for s in steps] == [
        (0, "tool_call", "steered"),
        (1, "tool_call", "ok"),
        (2, "tool_call", "error"),
    ]
    s0, s1, s2 = steps
    assert s0[3] == s0[4]  # D12: nothing applied
    assert s0[5].startswith("diff --git a/tests/test_auth.py b/tests/test_auth.py\n")
    assert "\n-    assert b()\n" in s0[5]
    assert s0[8] == feedback and s0[9] == 1
    assert s1[3] == s0[4] and s1[4] != s1[3]
    assert "+++ b/src/ok.py" in s1[5] and "+token = '<REDACTED_SECRET>'" in s1[5]
    assert "<REDACTED_SECRET>" in s1[7] and "sk-zzz" not in s1[7]
    assert (s1[9], s1[10]) == (0, None)
    assert (s2[9], s2[10], s2[3], s2[4]) == (1, 4, s1[4], s1[4])
    for sha in {s0[3], s1[4]}:
        assert git(repo, "cat-file", "-t", sha).strip() == "tree"


def test_agent_rr_tape_path_is_shared(repo: Path, tmp_path: Path) -> None:
    """D11: another writer created the tape (meta + step 0); flywire appends step 1."""
    tape = tmp_path / "shared.tape"
    conn = sqlite3.connect(tape)
    conn.executescript(TAPE_SCHEMA)
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    conn.execute(
        "INSERT INTO tape_meta (tape_id, created_at, command_argv, repo_root, base_commit_sha, "
        "metadata_json) VALUES ('t1', 1.0, '[]', ?, ?, '{\"producer\":\"agent-rr\"}')",
        (str(repo), git(repo, "rev-parse", "HEAD").strip()),
    )
    conn.execute(
        "INSERT INTO steps (step_idx, ts_start, ts_end, kind, pre_tree_sha, post_tree_sha) "
        "VALUES (0, 1.0, 2.0, 'llm_call', ?, ?)",
        (tree, tree),
    )
    conn.commit()
    conn.close()
    write(repo, "envelope.toml", TAPE_ENV)
    os.environ["AGENT_RR_TAPE_PATH"] = str(tape)
    try:
        w = Wrapped(repo)
    finally:
        del os.environ["AGENT_RR_TAPE_PATH"]
    w.call(1, "write_file", {"path": ".env", "content": "S=1\n"})
    assert w.close()[0] == 0
    assert tape_rows(tape, "SELECT tape_id FROM tape_meta") == [("t1",)]
    assert tape_rows(tape, "SELECT step_idx, kind, status FROM steps ORDER BY step_idx") == [
        (0, "llm_call", "ok"),
        (1, "tool_call", "steered"),
    ]
    assert not (repo / ".flywire/tapes").exists()


@pytest.mark.parametrize("commits", [False, True])
def test_no_git_or_no_commits_disables_tape_with_one_warning(tmp_path: Path, commits: bool) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    if commits:  # a git repo with no commits
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    write(root, "envelope.toml", TAPE_ENV)
    w = Wrapped(root)
    w.call(1, "write_file", {"path": ".env", "content": "x"})
    w.call(2, "write_file", {"path": "a.py", "content": "x"})
    code, stderr = w.close()
    assert code == 0
    assert stderr.count("tape emission disabled") == 1
    assert not (root / ".flywire/tapes").exists()
