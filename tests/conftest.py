from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from flywire.config import load_config
from flywire.engine import EnvelopeEngine, Interception
from flywire.models import ToolResult, Verdict


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, used as a workspace root."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("hello\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def make_engine(tmp_path: Path) -> Iterator[Callable[..., EnvelopeEngine]]:
    """Build a real engine (real config load, real SQLite WAL db) rooted at tmp_path."""
    engines: list[EnvelopeEngine] = []

    def factory(envelope: str = "", root: Path = tmp_path) -> EnvelopeEngine:
        if envelope:
            write(root, "envelope.toml", envelope)
        engine = EnvelopeEngine(load_config(root))
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        engine.close()


def write_call(engine: EnvelopeEngine, rel: str, text: str) -> Interception:
    """Propose a full-file write; apply it to disk only if flywire allows it (like upstream)."""
    call = engine.intercept("write_file", {"path": rel, "content": text})
    if call.verdict.action == "ALLOW":
        write(engine.root, rel, text)
    return call


def shell_call(engine: EnvelopeEngine, command: str, exit_code: int = 0, passed: int = 0) -> Verdict:
    call = engine.intercept("bash", {"command": command})
    if call.verdict.action == "ALLOW":
        engine.record_result(call, ToolResult(exit_code=exit_code, passed_count=passed))
    return call.verdict
