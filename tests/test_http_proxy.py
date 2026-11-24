from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import write

FLYWIRE = str(Path(sys.executable).parent / "flywire")
MOCK = str(Path(__file__).parent / "mock_http_server.py")
SSE_HEADERS = {"accept": "application/json, text/event-stream"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert proc.poll() is None, proc.communicate()
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} never opened")


class Servers:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log = root / "upstream.log"
        self.up_port, self.port = free_port(), free_port()
        env = {
            **os.environ,
            "MOCK_LOG": str(self.log),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        }
        self.upstream = subprocess.Popen(
            [sys.executable, MOCK, str(self.up_port)], cwd=root, env=env
        )
        wait_port(self.up_port, self.upstream)
        self.proxy = subprocess.Popen(
            [
                FLYWIRE,
                "serve",
                "--upstream",
                f"http://127.0.0.1:{self.up_port}",
                "--port",
                str(self.port),
            ],
            cwd=root,
            env=env,
            stderr=subprocess.PIPE,
        )
        wait_port(self.port, self.proxy)
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def post(self, frame: Any) -> httpx.Response:
        return httpx.post(self.url, json=frame, headers=SSE_HEADERS, timeout=15)

    def call(self, mid: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        resp = self.post(frame)
        if resp.headers["content-type"].startswith("text/event-stream"):
            data = [ln[6:] for ln in resp.text.splitlines() if ln.startswith("data: ")]
            return dict(json.loads(data[-1]))
        return dict(resp.json())

    def upstream_bodies(self) -> list[dict[str, Any]]:
        return (
            [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        )

    def stop(self) -> tuple[int, str]:
        self.proxy.send_signal(signal.SIGINT)
        code = self.proxy.wait(15)
        assert self.proxy.stderr is not None
        err = self.proxy.stderr.read().decode()
        self.upstream.terminate()
        self.upstream.wait(15)
        return code, err


@pytest.fixture
def servers(repo: Path) -> Iterator[Servers]:
    s = Servers(repo)
    yield s
    for proc in (s.proxy, s.upstream):
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_passthrough_intercept_allow(servers: Servers, repo: Path) -> None:
    write(repo, "tests/test_auth.py", "def test_a():\n    assert x\n    assert y\n")
    init = servers.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init.status_code == 200
    assert init.headers["mcp-session-id"] == "s-1"  # upstream headers relayed
    assert init.json()["result"]["serverInfo"]["name"] == "h"

    blocked = servers.call(
        "b-1",
        "write_file",
        {"path": "tests/test_auth.py", "content": "def test_a():\n    assert x\n"},
    )
    assert blocked["id"] == "b-1"
    assert blocked["result"]["isError"] is True
    assert blocked["result"]["content"][0]["text"].startswith("[FLYWIRE: py-no-deleted-assertions]")

    ok = servers.call(2, "write_file", {"path": "src/a.py", "content": "a = 1\n"})
    assert ok == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
    assert (repo / "src/a.py").read_text() == "a = 1\n"
    assert [b.get("id") for b in servers.upstream_bodies()] == [1, 2]

    code, err = servers.stop()
    assert code == 0
    assert "Traceback" not in err
    assert not (repo / ".flywire/flight_log.db-wal").exists()


def test_sse_results_feed_victory_gate(servers: Servers, repo: Path) -> None:
    servers.call(1, "write_file", {"path": "src/calc.py", "content": "x = 1\n"})
    steer = servers.call(2, "run_command", {"command": "git commit -m x"})
    assert steer["result"]["content"][0]["text"].startswith("[FLYWIRE: victory-unverified-commit]")
    write(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    run = servers.call(3, "run_command", {"command": "pytest -q -p no:cacheprovider tests"})
    assert run["result"]["structuredContent"]["exit_code"] == 0
    servers.call(4, "run_command", {"command": "git add -A"})
    commit = servers.call(5, "run_command", {"command": "git commit -q -m x"})
    assert commit["result"]["structuredContent"]["exit_code"] == 0


def test_sse_is_streamed_chunk_by_chunk(servers: Servers) -> None:
    start = time.monotonic()
    arrivals: list[float] = []
    with httpx.stream("GET", servers.url, headers={"accept": "text/event-stream"}, timeout=15) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for chunk in r.iter_text():
            if "notifications/tick" in chunk:
                arrivals.append(time.monotonic() - start)
    assert len(arrivals) == 2
    assert arrivals[0] < 0.8  # first event relayed before upstream finished its 1 s sleep
    assert arrivals[1] - arrivals[0] > 0.8


def test_batched_tools_call_rejected(servers: Servers) -> None:
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "write_file", "arguments": {"path": "b.py", "content": "x"}},
        }
    ]
    resp = servers.post(batch)
    assert resp.json() == [
        {
            "jsonrpc": "2.0",
            "id": 9,
            "error": {"code": -32600, "message": "flywire: batched tools/call is not supported"},
        }
    ]
    assert servers.upstream_bodies() == []


def test_serve_invalid_config_exits_2(repo: Path) -> None:
    write(repo, "envelope.toml", "[envelope]\nmode = 'yolo'\n")
    proc = subprocess.run(
        [FLYWIRE, "serve", "--upstream", "http://127.0.0.1:9", "--port", str(free_port())],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 2
    assert proc.stderr.startswith("error: ") and "Traceback" not in proc.stderr
