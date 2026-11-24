"""Premature victory gate: no `git commit` on unverified code (spec §5.5, contract D8)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from flywire.models import Verdict, VictorySection
from flywire.store.flight_db import SessionState

GIT_COMMIT = re.compile(r"^git\s+commit\b")
REMEDIATION = "Execute your test suite and confirm exit code 0 before committing."


def is_commit(units: Iterable[str]) -> bool:
    return any(GIT_COMMIT.match(u) for u in units)


def is_test_run(units: Iterable[str], patterns: Iterable[re.Pattern[str]]) -> bool:
    pats = tuple(patterns)
    return any(p.search(u) for u in units for p in pats)


def check_commit(
    cfg: VictorySection, session: SessionState, intervention_since_pass: bool
) -> Verdict | None:
    """Called only for commands containing `git commit`."""
    reason = None
    if cfg.block_commit_without_pass and session.last_code_edit_ts > session.last_test_pass_ts:
        reason = "Code was modified after the last passing test run."
    elif cfg.require_clean_check_before_commit and intervention_since_pass:
        reason = "A flywire intervention occurred after the last passing test run."
    if reason is None:
        return None
    return Verdict(
        action="STEER",
        rule_id="victory-unverified-commit",
        engine="victory_gate",
        reason=reason,
        remediation=REMEDIATION,
    )
