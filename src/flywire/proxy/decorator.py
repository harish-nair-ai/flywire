"""`@flywire.guard`: run the envelope in-process around a Python tool function (sync/async)."""

from __future__ import annotations

import atexit
import functools
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from flywire.config import find_root, load_config
from flywire.engine import EnvelopeEngine, Interception
from flywire.models import ToolResult, Verdict
from flywire.proxy.jsonrpc import tool_result

P = ParamSpec("P")
R = TypeVar("R")


class SteeringError(Exception):
    """Raised instead of running the tool; str() is the agent-facing steering text."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.feedback())
        self.verdict = verdict


@functools.cache
def _engine(root: Path) -> EnvelopeEngine:
    engine = EnvelopeEngine(load_config(root))
    atexit.register(engine.close)
    return engine


def _outcome(value: object) -> ToolResult:
    """Exit status of a shell-like return value: CompletedProcess, int exit code, or text."""
    code = getattr(value, "returncode", None)
    if isinstance(code, int):
        parts = (getattr(value, "stdout", None), getattr(value, "stderr", None))
        text = "".join(
            p.decode("utf-8", "replace") if isinstance(p, bytes) else p or "" for p in parts
        )
        return tool_result(code, text)
    if isinstance(value, int) and not isinstance(value, bool):
        return tool_result(value, "")
    return tool_result(0, str(value))


def guard(func: Callable[P, R]) -> Callable[P, R]:
    """Evaluate each call with the workspace's EnvelopeEngine (root found from cwd)."""
    signature = inspect.signature(func)

    def arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        out = dict(bound.arguments)
        for name, param in signature.parameters.items():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                out.update(out.pop(name, {}))
        return out

    def check(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[EnvelopeEngine, Interception]:
        engine = _engine(find_root(Path.cwd()))
        call = engine.intercept(func.__name__, arguments(args, kwargs))
        if call.verdict.action != "ALLOW":
            raise SteeringError(call.verdict)
        return engine, call

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            engine, call = check(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                engine.record_result(call, tool_result(1, str(exc)))
                raise
            engine.record_result(call, _outcome(result))
            return result

        return cast(Callable[P, R], async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        engine, call = check(args, kwargs)
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            engine.record_result(call, tool_result(1, str(exc)))
            raise
        engine.record_result(call, _outcome(result))
        return result

    return wrapper
