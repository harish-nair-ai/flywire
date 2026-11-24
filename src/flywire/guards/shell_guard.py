"""Shell command guard: contract §1.2 `shell_units` + first-firing regex rule."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from flywire.models import SEVERITY_ACTION, ShellRule, Verdict


@dataclass(frozen=True)
class CompiledShellRule:
    rule: ShellRule
    regex: re.Pattern[str]


def compile_shell_rule(rule: ShellRule) -> CompiledShellRule:
    try:
        return CompiledShellRule(rule, re.compile(rule.pattern))
    except re.error as exc:
        raise ValueError(f"rule {rule.id}: invalid pattern: {exc}") from exc


def _paren_end(cmd: str, i: int) -> int:
    """Index of the `)` closing a `$(` whose body starts at i (len(cmd) if unterminated)."""
    depth, quote = 1, ""
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(cmd)


def _backtick_end(cmd: str, i: int) -> int:
    while i < len(cmd) and cmd[i] != "`":
        i += 2 if cmd[i] == "\\" else 1
    return min(i, len(cmd))


def _norm(text: str) -> str:
    return " ".join(text.split())


def shell_units(cmd: str) -> list[str]:
    """Pipelines and simple commands of `cmd`, plus those of every `$(...)`/backtick body."""
    pipelines: list[tuple[int, int, list[tuple[int, int]]]] = []  # (start, end, commands)
    commands: list[tuple[int, int]] = []
    nested: list[str] = []
    pipe_start = cmd_start = i = 0
    quote = ""
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if quote == "'":
            quote = "" if c == "'" else quote
            i += 1
        elif c == "\\":
            i += 2
        elif cmd.startswith("$(", i):
            end = _paren_end(cmd, i + 2)
            nested.append(cmd[i + 2 : end])
            i = end + 1
        elif c == "`":
            end = _backtick_end(cmd, i + 1)
            nested.append(cmd[i + 1 : end])
            i = end + 1
        elif quote == '"':
            quote = "" if c == '"' else quote
            i += 1
        elif c in "'\"":
            quote = c
            i += 1
        else:
            two = cmd[i : i + 2]
            if c == "|" and two not in ("||", "|&"):
                commands.append((cmd_start, i))
                i += 1
                cmd_start = i
                continue
            if two == "|&":
                commands.append((cmd_start, i))
                i += 2
                cmd_start = i
                continue
            redirect = c == "&" and (two == "&>" or (i > 0 and cmd[i - 1] in "<>"))
            if two in ("&&", "||") or c in ";\n" or (c == "&" and not redirect):
                commands.append((cmd_start, i))
                pipelines.append((pipe_start, i, commands))
                i += 2 if two in ("&&", "||") else 1
                commands, pipe_start, cmd_start = [], i, i
                continue
            i += 1
    commands.append((cmd_start, n))
    pipelines.append((pipe_start, n, commands))

    units: list[str] = []
    for start, end, cmds in pipelines:
        simple = [u for u in (_norm(cmd[a:b]) for a, b in cmds) if u]
        if len(simple) > 1:
            units.append(_norm(cmd[start:end]))
        units += simple
    for body in nested:
        units += shell_units(body)
    return units


def evaluate_shell(rules: Iterable[CompiledShellRule], units: list[str]) -> Verdict | None:
    """First rule (load order) whose pattern matches any unit."""
    for compiled in rules:
        if any(compiled.regex.search(u) for u in units):
            rule = compiled.rule
            return Verdict(
                action=SEVERITY_ACTION[rule.severity],
                rule_id=rule.id,
                engine="shell_guard",
                reason=rule.message,
                remediation=rule.remediation,
            )
    return None
