from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, write

from flywire.config import load_config
from flywire.engine import EnvelopeEngine
from flywire.models import EnvelopeSection, ScopeSection, StallSection, VictorySection

FLYWIRE = str(Path(sys.executable).parent / "flywire")
AUTH = "def test_a():\n    assert 1\n    assert 2\n    assert 3\n"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "200"}
    return subprocess.run(
        [FLYWIRE, *args], cwd=root, capture_output=True, text=True, env=env, timeout=60
    )


def assert_clean_error(proc: subprocess.CompletedProcess[str], code: int) -> None:
    assert proc.returncode == code, (proc.stdout, proc.stderr)
    assert proc.stderr.startswith("error: "), proc.stderr
    assert "Traceback" not in proc.stderr + proc.stdout


def test_version_and_help(tmp_path: Path) -> None:
    proc = run(tmp_path, "--version")
    assert (proc.returncode, proc.stdout) == (0, "flywire 0.1.0\n")
    for cmd in ([], ["init"], ["wrap"], ["serve"], ["check"], ["status"], ["log"]):
        assert run(tmp_path, *cmd, "--help").returncode == 0, cmd


# --- init ---------------------------------------------------------------------------


def test_init_creates_files_and_never_overwrites(repo: Path) -> None:
    sub = repo / "pkg/deep"
    sub.mkdir(parents=True)
    proc = run(sub, "init")
    assert proc.returncode == 0, proc.stderr
    envelope = repo / "envelope.toml"  # workspace root, not cwd
    assert (repo / ".flywire/.gitignore").read_text() == "flight_log.db*\ntapes/\n.tmp/\n"
    cfg = load_config(repo)
    assert cfg.envelope.envelope.packs == ("core/python-safety", "core/git-hygiene")
    # every other value is the canonical default (contract §3)
    assert cfg.envelope.scope == ScopeSection()
    assert cfg.envelope.stall_detector == StallSection()
    assert cfg.envelope.victory_gate == VictorySection()
    assert cfg.envelope.envelope.model_copy(update={"packs": ()}) == EnvelopeSection()
    assert cfg.packs_missing == ("core/python-safety", "core/git-hygiene")  # D7: still loads

    envelope.write_text("# mine\n")
    assert_clean_error(run(repo, "init"), 2)
    assert envelope.read_text() == "# mine\n"
    assert run(repo, "init", "--force", "--lang", "ts").returncode == 0
    assert load_config(repo).envelope.envelope.packs == (
        "core/typescript-safety",
        "core/git-hygiene",
    )
    assert not list(repo.glob("*.tmp")) and not list(repo.glob("tmp*"))


def test_init_after_antibody_pull_is_actionable_and_force_keeps_packs(repo: Path) -> None:
    envelope = repo / "envelope.toml"
    pulled = '[envelope]\nversion = "0.1"\npacks = ["core/python-safety", "core/x-y"]\n'
    envelope.write_text(pulled)  # what `antibody pull` writes when no envelope exists
    refused = run(repo, "init")
    assert_clean_error(refused, 2)
    assert "flywire init --force" in refused.stderr and "packs" in refused.stderr
    assert envelope.read_text() == pulled
    assert run(repo, "init", "--force").returncode == 0
    assert load_config(repo).envelope.envelope.packs == (
        "core/python-safety",
        "core/x-y",
        "core/git-hygiene",
    )
    assert "emit_tape = false" in envelope.read_text()


def test_init_rejects_unknown_lang(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--lang", "cobol").returncode == 2
    assert not (tmp_path / "envelope.toml").exists()


# --- check --diff ---------------------------------------------------------------------


@pytest.fixture
def project(repo: Path) -> Path:
    write(repo, "tests/test_auth.py", AUTH)
    write(repo, "src/app.py", "x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def test_check_clean_tree_passes(project: Path) -> None:
    proc = run(project, "check", "--diff")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_working_tree_violation(project: Path) -> None:
    write(project, "tests/test_auth.py", AUTH.replace("    assert 2\n", ""))
    proc = run(project, "check", "--diff")
    assert proc.returncode == 10
    assert "tests/test_auth.py: [FLYWIRE: py-no-deleted-assertions]" in proc.stdout


def test_check_staged_only_sees_index(project: Path) -> None:
    write(project, "tests/test_auth.py", AUTH.replace("    assert 2\n", ""))
    assert run(project, "check", "--diff", "--staged").returncode == 0
    git(project, "add", "tests/test_auth.py")
    assert run(project, "check", "--diff", "--staged").returncode == 10
    write(project, "tests/test_auth.py", AUTH)  # working tree fixed, index still bad
    assert run(project, "check", "--diff", "--staged").returncode == 10
    assert run(project, "check", "--diff").returncode == 0


def test_check_base_ref(project: Path) -> None:
    write(project, "src/app.py", "x = 1  # noqa\n")
    git(project, "commit", "-q", "-am", "suppress")
    assert run(project, "check", "--diff").returncode == 0  # vs HEAD: no change
    proc = run(project, "check", "--diff", "--base", "HEAD~1")
    assert proc.returncode == 10
    assert "src/app.py: [FLYWIRE: py-no-lint-suppression]" in proc.stdout


def test_check_ignores_flywire_runtime_dir(project: Path) -> None:
    # Installed packs (antigen fixtures with bad code) live in untracked .flywire/.
    for i in range(20):
        write(project, f".flywire/packs/core/p/evals/f{i}/positive_after.py", "x = 1  # noqa\n")
    proc = run(project, "check", "--diff")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_untracked_deleted_and_protected(project: Path) -> None:
    write(project, "src/new.py", "try:\n    f()\nexcept Exception:\n    pass\n")
    (project / "tests/test_auth.py").unlink()
    write(project, ".github/workflows/ci.yml", "on: push\n")
    proc = run(project, "check", "--diff")
    assert proc.returncode == 10
    assert "src/new.py: [FLYWIRE: py-no-bare-except-pass]" in proc.stdout
    assert "tests/test_auth.py: [FLYWIRE: py-no-deleted-assertions]" in proc.stdout
    assert ".github/workflows/ci.yml: [FLYWIRE: scope-protected-path]" in proc.stdout


def test_check_turn_limits(project: Path) -> None:
    write(
        project,
        "envelope.toml",
        "[scope]\nmax_lines_deleted_per_turn = 1\nmax_files_modified = 1\n",
    )
    git(project, "add", "envelope.toml")
    git(project, "commit", "-q", "-m", "cfg")
    write(project, "src/app.py", "")
    write(project, "src/b.py", "b = 1\n")
    write(project, "src/c.py", "c = 1\n")
    proc = run(project, "check", "--diff")
    assert proc.returncode == 10
    assert "[FLYWIRE: scope-blast-radius]" in proc.stdout
    write(project, "src/app.py", "x = 1\n")
    (project / "src/b.py").unlink()
    (project / "src/c.py").unlink()
    write(project, "tests/test_auth.py", "def test_a():\n    pass\n")
    disabled = run(project, "check", "--diff")
    assert "[FLYWIRE: scope-mass-deletion]" in disabled.stdout


def test_check_broken_syntax_is_skipped(project: Path) -> None:
    write(project, "src/app.py", "def broken(:\n")
    proc = run(project, "check", "--diff")
    assert proc.returncode == 0
    assert "does not parse cleanly" in proc.stderr


def test_check_errors(project: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    assert_clean_error(run(project, "check"), 2)  # --diff is the only mode
    assert_clean_error(run(project, "check", "--diff", "--base", "no-such-ref"), 2)
    outside = tmp_path_factory.mktemp("not-a-repo")
    write(outside, "envelope.toml", "")
    assert_clean_error(run(outside, "check", "--diff"), 2)
    write(
        project,
        "envelope.toml",
        "[[ast_rules]]\nid='x'\nlanguage='python'\npaths=['**']\n"
        "check='new_node_introduced'\nquery='(((('\nseverity='steer'\nmessage='m'\n"
        "remediation='r'\n",
    )
    proc = run(project, "check", "--diff")
    assert_clean_error(proc, 2)
    assert "invalid python query" in proc.stderr


# --- status / log ---------------------------------------------------------------------


def test_status_and_log(repo: Path) -> None:
    write(repo, "envelope.toml", "[envelope]\npacks = ['core/python-safety']\n")
    empty = run(repo, "log")
    assert empty.returncode == 0 and "no flight log" in empty.stdout
    with EnvelopeEngine(load_config(repo)) as engine:
        engine.evaluate("write_file", {"path": ".env", "content": "x"})
        engine.evaluate("write_file", {"path": "src/ok.py", "content": "x = 1\n"})
        engine.evaluate("bash", {"command": "git reset --hard"})
    status = run(repo, "status")
    assert status.returncode == 0, status.stderr
    for needle in [
        "steer",
        "10 AST",
        "6 shell",
        "core/python-safety",
        "not installed",
        "scope-protected-path",
        "sh-no-hard-reset",
    ]:
        assert needle in status.stdout, needle
    log = run(repo, "log", "--tail", "2")
    assert log.returncode == 0
    assert "src/ok.py" in log.stdout and "sh-no-hard-reset" in log.stdout
    assert "scope-protected-path" not in log.stdout  # only the last 2 events


def test_internal_error_is_reported_without_traceback(repo: Path) -> None:
    write(repo, ".flywire/flight_log.db", "this is not a sqlite database" * 100)
    proc = run(repo, "log")
    assert_clean_error(proc, 1)
    assert proc.stderr.startswith("error: internal: DatabaseError: ")


def test_wrap_requires_command(repo: Path) -> None:
    assert_clean_error(run(repo, "wrap"), 2)
