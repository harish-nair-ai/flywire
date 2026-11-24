from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import write

FLYWIRE = str(Path(sys.executable).parent / "flywire")
MOCK = str(Path(__file__).parent / "mock_mcp_server.py")
AUTH_TEST = 'def test_login():\n    assert a()\n    assert b()\n    assert c()\n'


class Wrapped:
    """A real `flywire wrap -- python mock_mcp_server.py` process on real pipes."""

    def __init__(self, root: Path, *flags: str) -> None:
        self.root = root
        self.log, self.out, self.pidfile = root / "mock.log", root / "mock.out", root / "mock.pid"
        env = {
            **os.environ,
            "MOCK_LOG": str(self.log),
            "MOCK_OUT": str(self.out),
            "MOCK_PID": str(self.pidfile),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        }
        self.proc = subprocess.Popen(
            [FLYWIRE, "wrap", *flags, "--", sys.executable, MOCK],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def send(self, frame: dict[str, Any]) -> bytes:
        line = json.dumps(frame).encode() + b"\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(line)
        return line

    def recv(self, timeout: float = 10.0) -> Any:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        assert ready, "no response from flywire wrap"
        line = self.proc.stdout.readline()
        assert line.endswith(b"\n"), line
        self.last_raw = line
        return json.loads(line)

    def call(self, mid: Any, name: str, args: dict[str, Any]) -> Any:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        return self.recv()

    def close(self, timeout: float = 15.0) -> tuple[int, str]:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout)
        return code, self.proc.stderr.read().decode()

    def upstream_lines(self) -> list[bytes]:
        return self.log.read_bytes().splitlines(keepends=True) if self.log.exists() else []

    def mock_alive(self) -> bool:
        try:
            os.kill(int(self.pidfile.read_text()), 0)
        except ProcessLookupError:
            return False
        return True


@pytest.fixture
def wrapped(repo: Path) -> Any:
    procs: list[Wrapped] = []

    def start(*flags: str, envelope: str = "") -> Wrapped:
        if envelope:
            write(repo, "envelope.toml", envelope)
        w = Wrapped(repo, *flags)
        procs.append(w)
        return w

    yield start
    for w in procs:
        if w.proc.poll() is None:
            w.proc.kill()
            w.proc.wait()


def test_passthrough_intercept_and_allow(wrapped: Any, repo: Path) -> None:
    write(repo, "tests/test_auth.py", AUTH_TEST)
    w = wrapped()

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}}
    sent = w.send(init)
    assert w.recv()["result"]["serverInfo"]["name"] == "mock"
    assert w.last_raw == w.out.read_bytes()  # upstream -> client byte-for-byte
    w.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    w.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in w.recv()["result"]["tools"]] == ["write_file", "run_command"]

    forbidden = w.call("req-7", "write_file", {
        "path": "tests/test_auth.py",
        "content": AUTH_TEST.replace("    assert b()\n", ""),
    })  # fmt: skip
    assert forbidden["id"] == "req-7"  # original JSON type echoed
    assert forbidden["result"]["isError"] is True
    text = forbidden["result"]["content"][0]["text"]
    assert text.startswith("[FLYWIRE: py-no-deleted-assertions] ")
    assert "\nAction required: " in text
    assert (repo / "tests/test_auth.py").read_text() == AUTH_TEST  # never reached upstream

    ok = w.call(3, "write_file", {"path": "src/new.py", "content": "x = 1\n"})
    assert ok == {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "ok"}]}}
    assert (repo / "src/new.py").read_text() == "x = 1\n"

    code, stderr = w.close()
    assert code == 0
    lines = w.upstream_lines()
    assert lines[0] == sent  # client -> upstream byte-for-byte
    assert [json.loads(x).get("id") for x in lines] == [1, None, 2, 3]  # forbidden call absent
    assert "STEER py-no-deleted-assertions" in stderr
    assert "Traceback" not in stderr
    assert not w.mock_alive()
    assert not (repo / ".flywire/flight_log.db-wal").exists()


def test_shell_results_feed_victory_gate(wrapped: Any, repo: Path) -> None:
    w = wrapped()
    assert "result" in w.call(1, "write_file", {"path": "src/calc.py", "content": "x = 1\n"})
    blocked = w.call(2, "run_command", {"command": "git commit -m x"})
    assert blocked["result"]["content"][0]["text"].startswith("[FLYWIRE: victory-unverified-commit]")
    write(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    passed = w.call(3, "run_command", {"command": f"pytest -q -p no:cacheprovider {repo}/tests"})
    assert passed["result"]["structuredContent"]["exit_code"] == 0
    git_add = w.call(4, "run_command", {"command": "git add -A"})
    assert git_add["result"]["isError"] is False
    commit = w.call(5, "run_command", {"command": "git commit -q -m x"})
    assert commit["result"]["structuredContent"]["exit_code"] == 0
    assert w.close()[0] == 0
    conn = sqlite3.connect(repo / ".flywire/flight_log.db")
    rows = conn.execute("SELECT exit_code FROM tool_events WHERE op_class != 'FILE_WRITE'").fetchall()
    conn.close()
    assert rows == [(None,), (0,), (0,), (0,)]  # the steered commit never executed


def test_max_hard_blocks_exits_42(wrapped: Any) -> None:
    w = wrapped(envelope="[envelope]\nmax_hard_blocks = 2\n")
    for i in range(2):
        reply = w.call(i, "write_file", {"path": f"../escape{i}.py", "content": "x"})
        assert reply["result"]["content"][0]["text"].startswith("[FLYWIRE: scope-path-traversal]")
    code = w.proc.wait(15)
    assert code == 42
    assert not w.mock_alive()


def test_mode_flag_overrides_config(wrapped: Any) -> None:
    w = wrapped("--mode", "audit")
    reply = w.call(1, "write_file", {"path": ".env", "content": "S=1\n"})
    assert reply["result"] == {"content": [{"type": "text", "text": "ok"}]}  # audit: forwarded
    assert w.close()[0] == 0


def test_child_exit_code_is_propagated(wrapped: Any) -> None:
    w = wrapped()
    w.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "crash", "arguments": {"code": 7}}})  # fmt: skip
    assert w.proc.wait(15) == 7


def test_sigterm_cleans_up_child(wrapped: Any) -> None:
    w = wrapped()
    w.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    w.recv()
    w.proc.send_signal(signal.SIGTERM)
    assert w.proc.wait(15) == 0
    deadline = time.monotonic() + 5
    while w.mock_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not w.mock_alive()


def test_batched_tools_call_is_rejected(wrapped: Any) -> None:
    w = wrapped()
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]  # fmt: skip
    assert w.proc.stdin is not None
    w.proc.stdin.write(json.dumps(batch).encode() + b"\n")
    assert w.recv() == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "flywire: batched tools/call is not supported"},
        }
    ]
    assert w.close()[0] == 0
    assert w.upstream_lines() == []


def test_missing_upstream_command_is_actionable(repo: Path) -> None:
    proc = subprocess.run(
        [FLYWIRE, "wrap", "--", "definitely-not-a-command-xyz"],
        cwd=repo,
        capture_output=True,
        text=True,
        input="",
        timeout=15,
    )
    assert proc.returncode == 2
    assert proc.stderr.startswith("error: ")
    assert "Traceback" not in proc.stderr
