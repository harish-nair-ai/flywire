"""Load envelope.toml + built-in, pack, and local rules (spec §4, contract §3)."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from flywire.guards.ast_guard import CompiledAstRule, compile_ast_rule
from flywire.guards.shell_guard import CompiledShellRule, compile_shell_rule
from flywire.models import AstRule, EnvelopeFile, RuleFile, ShellRule

ENVELOPE = "envelope.toml"
M = TypeVar("M", EnvelopeFile, RuleFile)


class ConfigError(Exception):
    """Invalid configuration; CLI maps it to exit code 2."""


@dataclass(frozen=True)
class Config:
    root: Path
    envelope: EnvelopeFile
    ast_rules: tuple[CompiledAstRule, ...]
    shell_rules: tuple[CompiledShellRule, ...]
    packs_loaded: tuple[str, ...]
    packs_missing: tuple[str, ...]


def find_root(start: Path) -> Path:
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ENVELOPE).exists() or (d / ".git").exists():
            return d
    return start


def _read_toml(text: str, origin: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{origin}: invalid TOML: {exc}") from exc


def _parse(model: type[M], data: dict[str, Any], origin: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{origin}: {exc}") from exc


def _builtin_rule_files() -> list[RuleFile]:
    folder = files("flywire") / "builtin_rules"
    paths = sorted((p for p in folder.iterdir() if p.name.endswith(".toml")), key=lambda p: p.name)
    return [_parse(RuleFile, _read_toml(p.read_text("utf-8"), p.name), p.name) for p in paths]


def _pack_rule_files(pack_dir: Path) -> list[RuleFile]:
    out = []
    for path in sorted((pack_dir / "rules").glob("*.toml")):
        rf = _parse(RuleFile, _read_toml(path.read_text("utf-8"), str(path)), str(path))
        if len(rf.ast_rules) + len(rf.shell_rules) != 1:
            raise ConfigError(f"{path}: pack rule files must contain exactly one rule")
        out.append(rf)
    return out


def load_config(root: Path) -> Config:
    env_path = root / ENVELOPE
    data = _read_toml(env_path.read_text("utf-8"), ENVELOPE) if env_path.exists() else {}
    envelope = _parse(EnvelopeFile, data, ENVELOPE)

    sources: list[RuleFile] = []
    if envelope.envelope.use_builtin_rules:
        sources += _builtin_rule_files()
    loaded, missing = [], []
    for pack_id in envelope.envelope.packs:
        pack_dir = root / ".flywire" / "packs" / pack_id
        if not pack_dir.is_dir():
            print(f"warning: pack {pack_id} not installed; skipping", file=sys.stderr)
            missing.append(pack_id)
            continue
        sources += _pack_rule_files(pack_dir)
        loaded.append(pack_id)
    sources.append(RuleFile(ast_rules=envelope.ast_rules, shell_rules=envelope.shell_rules))

    # dicts keep first-insertion order; a later same-id rule replaces the value in place.
    ast: dict[str, AstRule] = {}
    shell: dict[str, ShellRule] = {}
    for rf in sources:
        ast.update((r.id, r) for r in rf.ast_rules)
        shell.update((r.id, r) for r in rf.shell_rules)
    disabled = set(envelope.envelope.disabled_rules)
    try:
        compiled_ast = tuple(compile_ast_rule(r) for r in ast.values() if r.id not in disabled)
        compiled_sh = tuple(compile_shell_rule(r) for r in shell.values() if r.id not in disabled)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return Config(root, envelope, compiled_ast, compiled_sh, tuple(loaded), tuple(missing))
