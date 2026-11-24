"""`flywire wrap` in front of a real stdio server with the official filesystem server's shapes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import write
from test_stdio_proxy import Wrapped

FS = str(Path(__file__).parent / "mock_fs_server.py")
TEST = "def test_add():\n    assert add(2, 2) == 4\n    assert add(0, 0) == 0\n"
DROP = {"oldText": "    assert add(0, 0) == 0\n", "newText": ""}


@pytest.fixture
def fs(repo: Path) -> Iterator[Wrapped]:
    write(repo, "tests/test_calc.py", TEST)
    w = Wrapped(repo, server=FS)
    w.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"capabilities": {}}})
    w.recv()
    yield w
    if w.proc.poll() is None:
        w.proc.kill()
        w.proc.wait()


def text(resp: Any) -> str:
    return str(resp["result"]["content"][0]["text"])


def steered(resp: Any, rule: str) -> bool:
    return resp["result"].get("isError") is True and text(resp).startswith(f"[FLYWIRE: {rule}] ")


def forwarded_ids(w: Wrapped) -> list[Any]:
    return [json.loads(line).get("id") for line in w.upstream_lines()]


def test_edit_file_edits_list_deleting_assertion_is_steered(fs: Wrapped, repo: Path) -> None:
    abs_path = str(repo / "tests/test_calc.py")
    resp = fs.call(1, "edit_file", {"path": abs_path, "edits": [DROP]})
    assert steered(resp, "py-no-deleted-assertions"), resp
    rename = {"oldText": "test_add", "newText": "test_sum"}
    second = fs.call(2, "edit_file", {"path": abs_path, "edits": [rename, DROP]})
    assert steered(second, "py-no-deleted-assertions"), second
    assert (repo / "tests/test_calc.py").read_text() == TEST
    assert 1 not in forwarded_ids(fs) and 2 not in forwarded_ids(fs)


def test_edit_file_dry_run_and_benign_edits_pass_through(fs: Wrapped, repo: Path) -> None:
    dry = fs.call(1, "edit_file", {"path": "tests/test_calc.py", "edits": [DROP], "dryRun": True})
    assert "isError" not in dry["result"] and "-    assert add(0, 0) == 0" in text(dry)
    assert (repo / "tests/test_calc.py").read_text() == TEST
    bump = {"oldText": "add(2, 2) == 4", "newText": "add(2, 3) == 5"}
    ok = fs.call(2, "edit_file", {"path": "tests/test_calc.py", "edits": [bump]})
    assert "isError" not in ok["result"], ok
    assert "add(2, 3) == 5" in (repo / "tests/test_calc.py").read_text()


def test_edit_file_unreconstructable_edit_on_guarded_file_is_steered(fs: Wrapped) -> None:
    fuzzy = {"oldText": "assert add(0,0)==0", "newText": ""}  # server may match loosely
    resp = fs.call(1, "edit_file", {"path": "tests/test_calc.py", "edits": [fuzzy]})
    assert steered(resp, "scope-unverifiable-edit"), resp


def test_move_file_is_a_mutation_of_both_paths(fs: Wrapped, repo: Path) -> None:
    away = fs.call(
        1, "move_file", {"source": "tests/test_calc.py", "destination": "tests/calc.py.bak"}
    )
    assert steered(away, "py-no-deleted-assertions"), away
    assert (repo / "tests/test_calc.py").exists()
    out = fs.call(2, "move_file", {"source": "tests/test_calc.py", "destination": "../outside.py"})
    assert steered(out, "scope-path-traversal"), out
    write(repo, "src/sh.py", "import subprocess\nsubprocess.run('ls', shell=True)\n")
    into = fs.call(3, "move_file", {"source": "src/sh.py", "destination": "src/run.py"})
    assert steered(into, "py-no-shell-true"), into
    folder = fs.call(4, "move_file", {"source": "tests", "destination": "old"})
    assert steered(folder, "py-no-deleted-assertions"), folder
    assert (repo / "tests/test_calc.py").exists()


def test_create_directory(fs: Wrapped, repo: Path) -> None:
    ok = fs.call(1, "create_directory", {"path": "src/pkg"})
    assert "isError" not in ok["result"] and (repo / "src/pkg").is_dir()
    evil = fs.call(2, "create_directory", {"path": "/etc/flywire-evil"})
    assert steered(evil, "scope-path-traversal"), evil
