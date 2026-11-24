"""agent-rr `.tape` emission for `emit_tape = true` (contract §6, §6.1, §6.2, D11, D12)."""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from flywire.engine import Interception
from flywire.models import OperationClass
from flywire.proxy.jsonrpc import extract_tool_result, result_text
from flywire.store.flight_db import close_checkpointed, connect

TAPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tape_meta (
    tape_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '0.1',
    created_at REAL NOT NULL,
    command_argv TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    base_commit_sha TEXT NOT NULL,
    parent_tape_id TEXT,
    fork_step_idx INTEGER,
    chaos_seed INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS steps (
    step_idx INTEGER PRIMARY KEY,
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llm_call','tool_call','chaos_fault')),
    pre_tree_sha TEXT NOT NULL,
    post_tree_sha TEXT NOT NULL,
    diff_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error','steered','injected_fault'))
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    raw_sse_chunks TEXT,
    http_status INTEGER NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    latency_ms REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_text TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER
);
CREATE TABLE IF NOT EXISTS chaos_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    preset TEXT NOT NULL,
    seed INTEGER NOT NULL,
    target TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_step ON llm_calls(step_idx);
CREATE INDEX IF NOT EXISTS idx_tool_calls_step ON tool_calls(step_idx);
CREATE INDEX IF NOT EXISTS idx_chaos_events_step ON chaos_events(step_idx);
"""

SECRETS = [  # applied in this order (contract §6)
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]


def redact(text: str) -> str:
    for pattern in SECRETS:
        text = pattern.sub("<REDACTED_SECRET>", text)
    return text


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def snapshot_tree(root: Path) -> str:
    """§6.1: worktree -> tree SHA via a throwaway index; never touches .git/index or refs."""
    fd, index = tempfile.mkstemp()
    os.close(fd)
    os.unlink(index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-A", "--", ".", ":!.agent-rr", ":!.flywire", env=env)
        return _git(root, "write-tree", env=env)
    finally:
        for leftover in (index, index + ".lock"):
            with suppress(FileNotFoundError):
                os.unlink(leftover)


def proposed_diff(rel: str, pre: bytes, post: bytes) -> str:
    """git-style unified diff of a shadow edit (D12: the rejected proposal)."""
    a = pre.decode("utf-8", "replace").splitlines(keepends=True)
    b = post.decode("utf-8", "replace").splitlines(keepends=True)
    old = f"a/{rel}" if pre else "/dev/null"
    new = f"b/{rel}" if post else "/dev/null"
    body = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(a, b, old, new)
    )
    return f"diff --git a/{rel} b/{rel}\n{body}" if body else ""


class TapeWriter:
    def __init__(self, path: Path, root: Path, argv: list[str], session_id: str) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._tree = snapshot_tree(root)  # state before the first step
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(path)
        self.conn.isolation_level = None  # explicit BEGIN IMMEDIATE transactions (D11)
        self.conn.executescript(TAPE_SCHEMA)
        meta = json.dumps({"producer": "flywire", "session_id": session_id}, sort_keys=True)
        with self._immediate():  # first writer inserts the single meta row
            self.conn.execute(
                "INSERT INTO tape_meta (tape_id, created_at, command_argv, repo_root, "
                "base_commit_sha, metadata_json) SELECT ?, ?, ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM tape_meta)",
                (uuid.uuid4().hex, time.time(), json.dumps(argv), str(root),
                 _git(root, "rev-parse", "HEAD"), meta),
            )  # fmt: skip

    @classmethod
    def open(cls, root: Path, argv: list[str], session_id: str) -> TapeWriter | None:
        """None (with one stderr warning) when root is not a git repo with commits."""
        try:
            _git(root, "rev-parse", "--verify", "HEAD")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(
                "warning: not a git repository with commits; tape emission disabled",
                file=sys.stderr,
            )
            return None
        override = os.environ.get("AGENT_RR_TAPE_PATH")
        path = Path(override) if override else root / ".flywire" / "tapes" / f"{session_id}.tape"
        return cls(path, root, argv, session_id)

    def close(self) -> None:
        close_checkpointed(self.conn)

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """D11: writers serialize on BEGIN IMMEDIATE; step rows commit atomically."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _append(
        self,
        ts_start: float,
        pre: str,
        post: str,
        diff: str,
        status: str,
        tool: tuple[str, dict[str, Any], str, bool, int | None],
    ) -> None:
        name, args, text, is_error, exit_code = tool
        with self._immediate():
            (step,) = self.conn.execute(
                "SELECT COALESCE(MAX(step_idx), -1) + 1 FROM steps"
            ).fetchone()
            self.conn.execute(
                "INSERT INTO steps (step_idx, ts_start, ts_end, kind, pre_tree_sha, "
                "post_tree_sha, diff_text, status) VALUES (?, ?, ?, 'tool_call', ?, ?, ?, ?)",
                (step, ts_start, time.time(), pre, post, redact(diff), status),
            )
            self.conn.execute(
                "INSERT INTO tool_calls (step_idx, tool_name, arguments_json, result_text, "
                "is_error, exit_code) VALUES (?, ?, ?, ?, ?, ?)",
                (step, name, redact(json.dumps(args, sort_keys=True, default=repr)),
                 redact(text), int(is_error), exit_code),
            )  # fmt: skip

    def record_steered(
        self, call: Interception, tool_name: str, args: dict[str, Any], ts_start: float
    ) -> None:
        """Run after the steer response is flushed. Nothing was applied: pre == post."""
        intent = call.intent
        diff = ""
        if intent and intent.target_path and intent.post_bytes is not None:
            diff = proposed_diff(intent.target_path, intent.pre_bytes or b"", intent.post_bytes)
        with self._lock:
            self._tree = snapshot_tree(self.root)
            tool = (tool_name, args, call.verdict.feedback(), True, None)
            self._append(ts_start, self._tree, self._tree, diff, "steered", tool)

    def record_allowed(
        self,
        call: Interception,
        tool_name: str,
        args: dict[str, Any],
        ts_start: float,
        response: dict[str, Any],
    ) -> None:
        """Run after the upstream response was relayed."""
        result = response.get("result")
        is_error = "error" in response or (isinstance(result, dict) and bool(result.get("isError")))
        shell = call.intent is not None and call.intent.op_class in (
            OperationClass.SHELL_EXEC,
            OperationClass.GIT_MUTATE,
        )
        exit_code = extract_tool_result(response).exit_code if shell else None
        with self._lock:
            # ponytail: pre = tree at the end of the previous step; edits made outside
            # flywire between steps are attributed to this step. Per-call pre-snapshots
            # would add git latency in front of every forwarded call.
            pre, post = self._tree, snapshot_tree(self.root)
            self._tree = post
            diff = _git(self.root, "diff-tree", "-p", pre, post) + "\n" if pre != post else ""
            tool = (tool_name, args, result_text(response), is_error, exit_code)
            self._append(ts_start, pre, post, diff, "error" if is_error else "ok", tool)
