from __future__ import annotations

from collections.abc import Callable

from conftest import shell_call, write_call

from flywire.engine import EnvelopeEngine

Factory = Callable[..., EnvelopeEngine]


def test_commit_after_edit_steers_until_tests_pass(make_engine: Factory) -> None:
    engine = make_engine()
    assert write_call(
        engine, "src/calc.py", "def add(a, b):\n    return a + b\n"
    ).verdict.action == ("ALLOW")
    blocked = shell_call(engine, 'git commit -m "fix"')
    assert (blocked.action, blocked.rule_id, blocked.engine) == (
        "STEER",
        "victory-unverified-commit",
        "victory_gate",
    )
    assert blocked.feedback().endswith(
        "Execute your test suite and confirm exit code 0 before committing."
    )
    assert "Code was modified after the last passing test run." in blocked.reason
    assert shell_call(engine, "pytest", exit_code=0, passed=3).action == "ALLOW"
    assert shell_call(engine, 'git commit -m "fix"').action == "ALLOW"


def test_failing_tests_do_not_unlock_commit(make_engine: Factory) -> None:
    engine = make_engine()
    write_call(engine, "src/calc.py", "x = 1\n")
    shell_call(engine, "uv run pytest -q", exit_code=1)
    assert (
        shell_call(engine, "git add -A && git commit -m x").rule_id == "victory-unverified-commit"
    )


def test_compound_test_command_counts(make_engine: Factory) -> None:
    engine = make_engine()
    write_call(engine, "src/calc.py", "x = 1\n")
    shell_call(engine, "cd pkg && npm test", exit_code=0)
    assert shell_call(engine, "git commit -am x").action == "ALLOW"


def test_commit_without_any_edit_is_allowed(make_engine: Factory) -> None:
    assert shell_call(make_engine(), 'git commit -m "docs"').action == "ALLOW"


def test_d8_intervention_since_last_pass_blocks_commit(make_engine: Factory) -> None:
    engine = make_engine()
    assert shell_call(engine, "pytest", exit_code=0).action == "ALLOW"
    assert engine.evaluate("write_file", {"path": ".env", "content": "x"}).action == "STEER"
    verdict = shell_call(engine, "git commit -m x")
    assert verdict.rule_id == "victory-unverified-commit"
    assert "intervention" in verdict.reason
    shell_call(engine, "pytest", exit_code=0)
    assert shell_call(engine, "git commit -m x").action == "ALLOW"


def test_switches(make_engine: Factory) -> None:
    engine = make_engine(
        "[victory_gate]\nblock_commit_without_pass = false\n"
        "require_clean_check_before_commit = false\n"
    )
    write_call(engine, "src/calc.py", "x = 1\n")
    assert shell_call(engine, "git commit -m x").action == "ALLOW"
    off = make_engine("[victory_gate]\nenabled = false\n")
    write_call(off, "src/calc2.py", "x = 1\n")
    assert shell_call(off, "git commit -m x").action == "ALLOW"
