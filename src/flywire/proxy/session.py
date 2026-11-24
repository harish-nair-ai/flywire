"""Transport-independent tools/call bookkeeping shared by the stdio and HTTP proxies."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from flywire.engine import EnvelopeEngine, Interception
from flywire.models import ToolCallRequest
from flywire.proxy.jsonrpc import extract_tool_result
from flywire.store.tape import TapeWriter


@dataclass(frozen=True)
class Pending:
    call: Interception
    request: ToolCallRequest
    ts_start: float


def id_key(request_id: Any) -> str:
    return json.dumps(request_id)  # keeps 1 and "1" distinct


class ProxySession:
    def __init__(self, engine: EnvelopeEngine, tape: TapeWriter | None) -> None:
        self.engine = engine
        self.tape = tape
        self.pending: dict[str, Pending] = {}
        self.calls = 0
        self.interventions = 0

    def intercept(self, request: ToolCallRequest) -> Pending:
        """Evaluate a tools/call; ALLOWed calls are tracked until their response arrives."""
        ts = time.time()
        name, args = request.params.name, request.params.arguments
        call = self.engine.intercept(name, args)
        self.calls += 1
        verdict = call.verdict
        if verdict.rule_id is not None:
            self.interventions += 1
            label = verdict.action if verdict.action != "ALLOW" else "AUDIT"
            target = call.intent.target_path if call.intent else args.get("path")
            print(
                f"flywire: {label} {verdict.rule_id} tool={name} target={target} "
                f"({verdict.latency_ms:.2f} ms)",
                file=sys.stderr,
                flush=True,
            )
        pending = Pending(call, request, ts)
        if verdict.action == "ALLOW":
            self.pending[id_key(request.id)] = pending
        return pending

    def on_response(self, msg: object) -> tuple[Pending, dict[str, Any]] | None:
        """Record a tracked call's result. Call BEFORE relaying the response so the agent's
        next request (e.g. `git commit` right after `pytest`) sees the updated state."""
        if not self.pending or not isinstance(msg, dict) or "id" not in msg:
            return None
        if "result" not in msg and "error" not in msg:
            return None
        pending = self.pending.pop(id_key(msg["id"]), None)
        if pending is None:
            return None
        self.engine.record_result(pending.call, extract_tool_result(msg))
        return pending, msg

    async def tape_steered(self, pending: Pending) -> None:
        if self.tape is not None:
            req = pending.request
            await self._tape(
                self.tape.record_steered,
                pending.call,
                req.params.name,
                req.params.arguments,
                pending.ts_start,
            )

    async def tape_allowed(self, pending: Pending, msg: dict[str, Any]) -> None:
        if self.tape is not None:
            req = pending.request
            await self._tape(
                self.tape.record_allowed,
                pending.call,
                req.params.name,
                req.params.arguments,
                pending.ts_start,
                msg,
            )

    @staticmethod
    async def _tape(fn: Any, *args: Any) -> None:
        """Snapshot + insert off the event loop; a tape failure never breaks the proxy."""
        try:
            await asyncio.to_thread(fn, *args)
        except (subprocess.CalledProcessError, sqlite3.Error, OSError) as exc:
            print(f"warning: tape step not recorded: {exc}", file=sys.stderr, flush=True)

    def summary(self) -> str:
        return (
            f"flywire: session {self.engine.session_id}: {self.calls} tool call(s), "
            f"{self.interventions} intervention(s)"
        )
