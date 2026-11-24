from __future__ import annotations

import math
import socket
import sqlite3
import time
from collections.abc import Callable
from typing import Any

import pytest
from conftest import write

from flywire.engine import EnvelopeEngine

ITERATIONS = 200
SLA_P99_MS = 15.0


def big_test_file(lines: int = 500) -> str:
    """A realistic 500-line pytest module that exercises all 5 Python rule queries."""
    out = ["import subprocess", "import requests", ""]
    tail = ["", "def helper():", "    return 1"]
    i = 0
    while len(out) + 10 + len(tail) <= lines:  # whole blocks only: the file must parse
        out += [
            f"def test_case_{i}(client):",
            f"    resp = requests.get('https://x/{i}', timeout=5)  # check {i}",
            "    try:",
            f"        value = compute({i}, retries=3)",
            "    except ValueError:",
            "        value = None",
            f"    assert resp.status_code == 200, 'case {i}'",
            "    self_check = client.assertEqual(value, value)",
            f"    subprocess.run(['echo', '{i}'], check=True)",
            "",
        ]
        i += 1
    out += [f"# padding {n}" for n in range(lines - len(out) - len(tail))] + tail
    return "\n".join(out) + "\n"


def p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(0.99 * len(ordered)) - 1]


def test_p99_under_15ms_with_all_ast_rules_and_sqlite(
    make_engine: Callable[..., EnvelopeEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access in the hot path")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    engine = make_engine()
    assert len(engine.config.ast_rules) == 10
    source = big_test_file()
    assert source.count("\n") == 500
    write(engine.root, "tests/test_big.py", source)

    latencies: list[float] = []
    verdicts: set[str] = set()
    for i in range(ITERATIONS):
        if i % 2:  # search/replace edit (shadow patch from disk)
            args = {
                "path": "tests/test_big.py",
                "old_str": f"# check {i}",
                "new_str": f"# checked {i}",
            }
            tool = "str_replace_editor"
        else:  # full-file write
            args = {"path": "tests/test_big.py", "content": source.replace("x/0'", f"x/{i}'")}
            tool = "write_file"
        start = time.perf_counter()
        verdict = engine.evaluate(tool, args)
        latencies.append((time.perf_counter() - start) * 1000)
        verdicts.add(verdict.action)

    conn = sqlite3.connect(engine.root / ".flywire/flight_log.db")
    (logged,) = conn.execute("SELECT COUNT(*) FROM tool_events").fetchone()
    conn.close()
    assert logged == ITERATIONS  # every evaluation hit SQLite
    measured = p99(latencies)
    print(
        f"\nlatency over {ITERATIONS} evaluate() calls: "
        f"p50={sorted(latencies)[ITERATIONS // 2]:.3f} ms, p99={measured:.3f} ms, "
        f"max={max(latencies):.3f} ms, verdicts={sorted(verdicts)}"
    )
    assert measured < SLA_P99_MS


def test_ast_violation_detected_on_big_file(make_engine: Callable[..., EnvelopeEngine]) -> None:
    """Guard against a benchmark that is fast only because rules silently skip."""
    engine = make_engine()
    source = big_test_file()
    write(engine.root, "tests/test_big.py", source)
    edited = source.replace("    assert resp.status_code == 200, 'case 3'\n", "", 1)
    verdict = engine.evaluate("write_file", {"path": "tests/test_big.py", "content": edited})
    assert verdict.rule_id == "py-no-deleted-assertions"
