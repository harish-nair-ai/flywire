"""EnvelopeEngine: classify -> guards (short-circuit) -> mode -> flight log (spec §2)."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from flywire.classifier import PathTraversalError, classify
from flywire.config import Config
from flywire.guards import scope_guard, stall_detector, victory_gate
from flywire.guards.ast_guard import evaluate_ast, gitwildmatch
from flywire.guards.shell_guard import evaluate_shell, shell_units
from flywire.models import Mode, OperationClass, ToolCallIntent, ToolResult, Verdict, VerdictAction
from flywire.store.flight_db import FlightDB

WRITE = OperationClass.FILE_WRITE


@dataclass(frozen=True)
class Interception:
    verdict: Verdict
    intent: ToolCallIntent | None  # None only for a path-traversal HARD_BLOCK
    event_id: int | None  # None for READ_ONLY calls (never logged)


class EnvelopeEngine:
    def __init__(self, config: Config, mode: Mode | None = None) -> None:
        self.config = config
        self.root: Path = config.root.resolve()
        env = config.envelope
        self.mode: Mode = mode or env.envelope.mode
        self.session_id = uuid.uuid4().hex
        self._disabled = frozenset(env.envelope.disabled_rules)
        self._protected = gitwildmatch(env.scope.protected_paths)
        self._test_patterns = tuple(re.compile(p) for p in env.victory_gate.test_command_patterns)
        self._modified: set[str] = set()
        self._step = 0
        self._hard_blocks = 0
        self._lock = threading.Lock()
        self.db = FlightDB(self.root / ".flywire" / "flight_log.db")
        self.db.start_session(self.session_id, str(self.root), self.mode, time.time())

    @property
    def consecutive_hard_blocks(self) -> int:
        return self._hard_blocks

    def evaluate(self, tool_name: str, args: dict[str, Any]) -> Verdict:
        return self.intercept(tool_name, args).verdict

    def intercept(self, tool_name: str, args: dict[str, Any]) -> Interception:
        with self._lock:
            t0 = time.perf_counter()
            try:
                intent = classify(tool_name, args, self.root)
            except PathTraversalError as exc:
                verdict = scope_guard.path_traversal(exc.path)
                return self._finish(t0, args, tool_name, None, verdict, exc.path)
            if intent.op_class is OperationClass.READ_ONLY:
                latency = (time.perf_counter() - t0) * 1000
                allow = Verdict(action="ALLOW", engine="envelope", latency_ms=latency)
                return Interception(allow, intent, None)
            units = shell_units(intent.shell_command) if intent.shell_command else []
            violation = next(
                (
                    v
                    for v in self._checks(intent, units)
                    if v is not None and v.rule_id not in self._disabled
                ),
                None,
            )
            return self._finish(t0, args, tool_name, intent, violation, intent.target_path)

    def record_result(self, call: Interception, result: ToolResult) -> None:
        """Feed an executed shell/git call's outcome back (stall error loop, victory gate)."""
        intent = call.intent
        if call.event_id is None or intent is None or intent.shell_command is None:
            return
        with self._lock:
            failed_hash = result.error_hash if result.exit_code != 0 else None
            self.db.set_result(call.event_id, exit_code=result.exit_code, error_hash=failed_hash)
            units = shell_units(intent.shell_command)
            if result.exit_code == 0 and victory_gate.is_test_run(units, self._test_patterns):
                self.db.set_test_pass(self.session_id, time.time(), result.passed_count)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> EnvelopeEngine:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------------

    def _checks(self, intent: ToolCallIntent, units: list[str]) -> Iterator[Verdict | None]:
        """Guards in short-circuit order; consumed lazily until the first violation."""
        env = self.config.envelope
        rel = intent.target_path
        if intent.op_class is WRITE:
            if rel is not None and intent.pre_bytes is not None:
                yield scope_guard.protected_path(self._protected, rel)
                if intent.post_bytes is not None:
                    deleted = scope_guard.lines_deleted(intent.pre_bytes, intent.post_bytes)
                    yield scope_guard.mass_deletion(deleted, env.scope.max_lines_deleted_per_turn)
                distinct = len(self._modified | {rel})
                yield scope_guard.blast_radius(distinct, env.scope.max_files_modified)
                if intent.post_bytes is not None:
                    yield evaluate_ast(
                        self.config.ast_rules, rel, intent.pre_bytes, intent.post_bytes
                    )
        else:
            yield evaluate_shell(self.config.shell_rules, units)

        stall = env.stall_detector
        if stall.enabled:
            execs = self.db.recent_executions(self.session_id, stall.max_consecutive_tool_errors)
            reported = bool(execs) and self.db.rule_fired_since(
                self.session_id, stall_detector.RULE_ID, execs[0][0]
            )
            yield stall_detector.check_errors(stall, execs, reported)
            if intent.op_class is WRITE and rel is not None:
                window = self.db.recent_writes(
                    self.session_id, stall.window_size - 1, audit=self.mode == "audit"
                )
                last_pass = self.db.session(self.session_id).last_test_pass_ts
                yield stall_detector.check_write(
                    stall, window, rel, self._state_hash(intent), last_pass
                )

        gate = env.victory_gate
        if gate.enabled and victory_gate.is_commit(units):
            session = self.db.session(self.session_id)
            dirty = self.db.intervention_since(self.session_id, session.last_test_pass_ts)
            yield victory_gate.check_commit(gate, session, dirty)

    @staticmethod
    def _state_hash(intent: ToolCallIntent) -> str | None:
        if intent.target_path is None or intent.post_bytes is None:
            return None
        return stall_detector.state_hash(intent.target_path, intent.post_bytes)

    def _finish(
        self,
        t0: float,
        args: dict[str, Any],
        tool_name: str,
        intent: ToolCallIntent | None,
        violation: Verdict | None,
        target: str | None,
    ) -> Interception:
        raw = violation or Verdict(action="ALLOW", engine="envelope")
        logged: VerdictAction = (
            "HARD_BLOCK" if self.mode == "strict" and raw.action == "STEER" else raw.action
        )
        action: VerdictAction = "ALLOW" if self.mode == "audit" else logged
        verdict = raw.model_copy(
            update={"action": action, "latency_ms": (time.perf_counter() - t0) * 1000}
        )

        ts = time.time()
        event_id = self.db.insert_event(
            session_id=self.session_id,
            step_idx=self._step,
            ts=ts,
            tool_name=tool_name,
            op_class=(intent.op_class if intent else WRITE).value,
            target_path=target,
            state_hash=self._state_hash(intent) if intent else None,
            verdict=logged,
            rule_id=raw.rule_id,
            reason=raw.reason or None,
            latency_ms=verdict.latency_ms,
            args_json=json.dumps(args, sort_keys=True, default=repr),
        )
        self._step += 1
        if action == "ALLOW" and intent and intent.op_class is WRITE and intent.target_path:
            self._modified.add(intent.target_path)
            self.db.set_code_edit(self.session_id, ts)
        hard_blocks = (
            self._hard_blocks + 1 if action == "HARD_BLOCK" else 0 if action == "ALLOW" else None
        )
        if hard_blocks is not None and hard_blocks != self._hard_blocks:
            self._hard_blocks = hard_blocks
            self.db.set_hard_blocks(self.session_id, hard_blocks)
        return Interception(verdict, intent, event_id)
