from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, write

import flywire

AUTH = "def test_a():\n    assert 1\n    assert 2\n"


@flywire.guard
def write_file(path: str, content: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)
    return "ok"


@flywire.guard
async def edit_file(path: str, old_str: str, new_str: str) -> str:
    p = Path(path)
    p.write_text(p.read_text().replace(old_str, new_str, 1))
    return "edited"


@flywire.guard
def bash(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, capture_output=True, text=True)


@pytest.fixture
def workspace(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:{os.environ['PATH']}")
    write(repo, "tests/test_auth.py", AUTH)
    return repo


def test_sync_guard_blocks_and_allows(workspace: Path) -> None:
    with pytest.raises(flywire.SteeringError) as info:
        write_file("tests/test_auth.py", "def test_a():\n    assert 1\n")
    assert info.value.verdict.rule_id == "py-no-deleted-assertions"
    assert str(info.value).startswith("[FLYWIRE: py-no-deleted-assertions] ")
    assert (workspace / "tests/test_auth.py").read_text() == AUTH  # body never ran
    assert write_file("src/ok.py", "x = 1\n") == "ok"
    assert write_file.__name__ == "write_file"


async def test_async_guard(workspace: Path) -> None:
    assert inspect.iscoroutinefunction(edit_file)
    with pytest.raises(flywire.SteeringError):
        await edit_file(path="tests/test_auth.py", old_str="    assert 2\n", new_str="")
    assert await edit_file("tests/test_auth.py", "assert 2", "assert 3") == "edited"


def test_shell_results_are_recorded_with_real_pytest(workspace: Path) -> None:
    write_file("src/calc.py", "def add(a, b):\n    return a + b\n")
    with pytest.raises(flywire.SteeringError, match="victory-unverified-commit"):
        bash("git commit -m wip")
    write(workspace, "tests/test_calc.py", "def test_ok():\n    assert True\n")
    assert bash("pytest -q -p no:cacheprovider tests/test_calc.py").returncode == 0
    git(workspace, "add", "-A")
    assert bash("git commit -q -m wip").returncode == 0
