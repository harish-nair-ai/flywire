from __future__ import annotations

from collections.abc import Callable

from conftest import shell_call, write_call

from flywire.engine import EnvelopeEngine
from flywire.models import ToolResult

Factory = Callable[..., EnvelopeEngine]


def test_three_edits_without_test_progress_trigger_on_third(make_engine: Factory) -> None:
    engine = make_engine()
    assert write_call(engine, "src/app.py", "v = 1\n").verdict.action == "ALLOW"
    assert write_call(engine, "src/app.py", "v = 2\n").verdict.action == "ALLOW"
    third = write_call(engine, "src/app.py", "v = 3\n").verdict
    assert (third.action, third.rule_id, third.engine) == (
        "STEER",
        "stall-loop-detected",
        "stall_detector",
    )
    assert third.remediation == engine.config.envelope.stall_detector.steer_message


def test_ab_oscillation_on_step_three(make_engine: Factory) -> None:
    engine = make_engine("[stall_detector]\nmax_semantic_repeats = 10\n")
    assert write_call(engine, "src/app.py", "A\n").verdict.action == "ALLOW"
    assert write_call(engine, "src/app.py", "B\n").verdict.action == "ALLOW"
    back = write_call(engine, "src/app.py", "A\n").verdict
    assert (back.action, back.rule_id) == ("STEER", "stall-loop-detected")
    assert "oscillat" in back.reason


def test_oscillation_detection_can_be_disabled(make_engine: Factory) -> None:
    engine = make_engine(
        "[stall_detector]\nmax_semantic_repeats = 10\ndetect_ab_oscillation = false\n"
    )
    for text in ["A\n", "B\n", "A\n"]:
        assert write_call(engine, "src/app.py", text).verdict.action == "ALLOW"


def test_passing_test_resets_semantic_repeat_window(make_engine: Factory) -> None:
    engine = make_engine()
    write_call(engine, "src/app.py", "v = 1\n")
    write_call(engine, "src/app.py", "v = 2\n")
    assert shell_call(engine, "pytest -q", exit_code=0, passed=4).action == "ALLOW"
    assert write_call(engine, "src/app.py", "v = 3\n").verdict.action == "ALLOW"
    assert write_call(engine, "src/app.py", "v = 4\n").verdict.action == "ALLOW"
    assert write_call(engine, "src/app.py", "v = 5\n").verdict.rule_id == "stall-loop-detected"


def test_failing_test_does_not_reset(make_engine: Factory) -> None:
    engine = make_engine()
    write_call(engine, "src/app.py", "v = 1\n")
    write_call(engine, "src/app.py", "v = 2\n")
    shell_call(engine, "pytest -q", exit_code=1)
    assert write_call(engine, "src/app.py", "v = 3\n").verdict.rule_id == "stall-loop-detected"


def test_window_size_bounds_repeat_count(make_engine: Factory) -> None:
    engine = make_engine("[stall_detector]\nwindow_size = 3\n")
    write_call(engine, "src/app.py", "v = 1\n")
    write_call(engine, "src/other.py", "o = 1\n")
    write_call(engine, "src/more.py", "m = 1\n")
    # only the last 2 recorded writes + this one are in the window: app.py appears once
    assert write_call(engine, "src/app.py", "v = 2\n").verdict.action == "ALLOW"


def test_consecutive_identical_errors_fire_once(make_engine: Factory) -> None:
    engine = make_engine()
    err = "FAILED tests/test_a.py:12: at 2026-11-24T10:00:01Z in /tmp/pytest-of-x/1/a.py"
    for i in range(3):
        call = engine.intercept("bash", {"command": "make build"})
        assert call.verdict.action == "ALLOW"
        text = err.replace(":12:", f":{12 + i}:").replace("10:00:01", f"10:00:0{i}")
        engine.record_result(call, ToolResult(exit_code=2, error_hash=engine_error_hash(text)))
    verdict = engine.evaluate("bash", {"command": "make build"})
    assert (verdict.action, verdict.rule_id) == ("STEER", "stall-loop-detected")
    assert "identical" in verdict.reason
    # the agent must not be locked out forever: the loop is reported once per error streak
    assert engine.evaluate("bash", {"command": "cat build.log"}).action == "ALLOW"


def test_different_errors_do_not_fire(make_engine: Factory) -> None:
    engine = make_engine()
    for i in range(3):
        call = engine.intercept("bash", {"command": f"make t{i}"})
        engine.record_result(call, ToolResult(exit_code=1, error_hash=f"h{i}"))
    assert engine.evaluate("bash", {"command": "make"}).action == "ALLOW"


def test_disabled_stall_detector(make_engine: Factory) -> None:
    engine = make_engine("[stall_detector]\nenabled = false\n")
    for i in range(5):
        assert write_call(engine, "src/app.py", f"v = {i}\n").verdict.action == "ALLOW"


def engine_error_hash(text: str) -> str:
    from flywire.proxy.jsonrpc import error_hash

    return error_hash(text)
