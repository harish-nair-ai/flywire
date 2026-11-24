"""Rolling-window stall detector: semantic repeat, A-B oscillation, error loop (spec §5.4)."""

from __future__ import annotations

import hashlib

from flywire.models import StallSection, Verdict
from flywire.store.flight_db import WriteRow

RULE_ID = "stall-loop-detected"


def state_hash(rel_path: str, post: bytes) -> str:
    inner = hashlib.sha256(post).hexdigest()
    return hashlib.sha256(f"{rel_path}:{inner}".encode()).hexdigest()


def _steer(cfg: StallSection, reason: str) -> Verdict:
    return Verdict(
        action="STEER",
        rule_id=RULE_ID,
        engine="stall_detector",
        reason=reason,
        remediation=cfg.steer_message,
    )


def check_write(
    cfg: StallSection,
    window: list[WriteRow],
    rel_path: str,
    proposed_hash: str | None,
    last_test_pass_ts: float,
) -> Verdict | None:
    """`window` = the previous `window_size - 1` applied writes, newest first."""
    same_path = [w for w in window if w.target_path == rel_path]
    # same_path[0] is the immediately preceding state; an equal hash 2..W steps back is a revert.
    reverted = proposed_hash is not None and any(
        w.state_hash == proposed_hash for w in same_path[1:]
    )
    if cfg.detect_ab_oscillation and reverted:
        return _steer(cfg, f"'{rel_path}' is oscillating back to a state it already had.")
    repeats = 1 + sum(1 for w in same_path if w.ts > last_test_pass_ts)
    if repeats >= cfg.max_semantic_repeats:
        return _steer(
            cfg,
            f"'{rel_path}' was edited {repeats} times in the last {cfg.window_size} edits "
            "without a passing test run.",
        )
    return None


def check_errors(
    cfg: StallSection, executions: list[tuple[int, int, str | None]], already_reported: bool
) -> Verdict | None:
    """`executions` = last `max_consecutive_tool_errors` (step, exit, error_hash), newest first.

    Reported once per streak so the agent can still investigate instead of being locked out.
    """
    if already_reported or len(executions) < cfg.max_consecutive_tool_errors:
        return None
    hashes = {h for _, code, h in executions if code != 0}
    if any(code == 0 for _, code, _ in executions) or len(hashes) != 1 or None in hashes:
        return None
    return _steer(
        cfg,
        f"The last {len(executions)} tool executions failed with the identical error.",
    )
