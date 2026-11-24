"""Shell command guard: contract §1.2 `shell_units` + first-firing regex rule."""

from __future__ import annotations

import re
from dataclasses import dataclass

from flywire.models import ShellRule


@dataclass(frozen=True)
class CompiledShellRule:
    rule: ShellRule
    regex: re.Pattern[str]


def compile_shell_rule(rule: ShellRule) -> CompiledShellRule:
    try:
        return CompiledShellRule(rule, re.compile(rule.pattern))
    except re.error as exc:
        raise ValueError(f"rule {rule.id}: invalid pattern: {exc}") from exc
