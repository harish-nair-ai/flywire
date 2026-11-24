"""Typer CLI: init, wrap, serve, check, status, log (spec §7, contract §0)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from flywire.config import Config, ConfigError, find_root, load_config
from flywire.engine import EnvelopeEngine
from flywire.guards import scope_guard
from flywire.guards.ast_guard import evaluate_ast, gitwildmatch
from flywire.models import Mode, Verdict
from flywire.store.flight_db import EventRow, FlightDB
from flywire.store.tape import TapeWriter

VERSION = "0.1.0"
EXIT_VIOLATIONS = 10
GITIGNORE = "flight_log.db*\ntapes/\n.tmp/\n"
PACKS = {"python": "core/python-safety", "ts": "core/typescript-safety"}
ENVELOPE_TEMPLATE = """\
[envelope]
version = "0.1"
mode = "steer"                 # "audit" | "steer" | "strict"
max_hard_blocks = 5            # Terminate proxy with exit code 42 after N consecutive blocks
packs = ["{pack}", "core/git-hygiene"]
disabled_rules = []            # Rule IDs to silence locally
emit_tape = false              # If true, writes agent-rr compatible steps into a .tape

[scope]
max_files_modified = 15        # Unique workspace-relative files mutated per session
max_lines_deleted_per_turn = 150
protected_paths = [
    ".github/workflows/**",
    "migrations/**",
    "*.lock",
    ".env*",
]

[stall_detector]
enabled = true
window_size = 5
max_semantic_repeats = 3       # Same file edited 3x in window without test progress
max_consecutive_tool_errors = 3
detect_ab_oscillation = true   # Blocks state_hash[t] == state_hash[t-2]
steer_message = \"\"\"\\
Repair loop detected. Stop editing, read the full error output, \\
and state a new root-cause hypothesis before modifying code.\"\"\"

[victory_gate]
enabled = true
test_command_patterns = [
    "^pytest\\\\b",
    "^uv run pytest\\\\b",
    "^npm test\\\\b",
    "^vitest\\\\b",
    "^cargo test\\\\b",
]
block_commit_without_pass = true
require_clean_check_before_commit = true
"""


class UsageError(Exception):
    """Bad arguments / missing prerequisites: exit 2."""


class FlywireApp(typer.Typer):
    """Maps every failure to `error: ...` on stderr; users never see a traceback."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, **kwargs)
        except (ConfigError, UsageError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"error: internal: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)


app = FlywireApp(
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=True,
    help="Sub-15ms code-aware flight envelope proxy for coding agents.",
)


def _version(value: bool) -> None:
    if value:
        print(f"flywire {VERSION}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    """flywire: deterministic envelope protection for coding agents."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            os.unlink(tmp.name)
            raise
    os.replace(tmp.name, path)


@app.command()
def init(
    lang: Annotated[
        Literal["python", "ts"], typer.Option(help="Starter pack language.")
    ] = "python",
    force: Annotated[bool, typer.Option(help="Overwrite an existing envelope.toml.")] = False,
) -> None:
    """Create a starter envelope.toml and .flywire/.gitignore in the workspace root."""
    root = find_root(Path.cwd())
    envelope = root / "envelope.toml"
    if envelope.exists() and not force:
        raise UsageError(f"{envelope} already exists (use --force to overwrite)")
    _atomic_write(envelope, ENVELOPE_TEMPLATE.format(pack=PACKS[lang]))
    _atomic_write(root / ".flywire" / ".gitignore", GITIGNORE)
    print(f"created {envelope} and {root / '.flywire' / '.gitignore'}")


def _workspace() -> Config:
    return load_config(find_root(Path.cwd()))


@app.command(context_settings={"ignore_unknown_options": True})
def wrap(
    command: Annotated[list[str] | None, typer.Argument(help="Upstream MCP command.")] = None,
    mode: Annotated[Mode | None, typer.Option(help="Override envelope mode.")] = None,
) -> None:
    """Run an MCP stdio server behind flywire: `flywire wrap -- <cmd...>`."""
    from flywire.proxy.stdio_proxy import run_stdio_proxy

    if not command:
        raise UsageError("missing upstream command: flywire wrap -- <cmd...>")
    config = _workspace()
    engine = EnvelopeEngine(config, mode=mode)
    tape = (
        TapeWriter.open(engine.root, command, engine.session_id)
        if config.envelope.envelope.emit_tape
        else None
    )
    try:
        code = asyncio.run(
            run_stdio_proxy(command, engine, tape, config.envelope.envelope.max_hard_blocks)
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise UsageError(f"cannot start upstream command {command[0]!r}: {exc}") from exc
    finally:
        if tape is not None:
            tape.close()
        engine.close()
    raise typer.Exit(code)


@app.command()
def serve(
    upstream: Annotated[str, typer.Option(help="Upstream MCP base URL.")],
    port: Annotated[int, typer.Option(help="Local port.")] = 8765,
) -> None:
    """Reverse-proxy a Streamable HTTP / SSE MCP server on 127.0.0.1:<port>."""
    import uvicorn

    from flywire.proxy.http_proxy import create_app

    config = _workspace()
    engine = EnvelopeEngine(config)
    argv = ["flywire", "serve", "--upstream", upstream]
    tape = (
        TapeWriter.open(engine.root, argv, engine.session_id)
        if config.envelope.envelope.emit_tape
        else None
    )
    try:
        uvicorn.run(
            create_app(engine, upstream, tape),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=2,
        )
    finally:
        if tape is not None:
            tape.close()
        engine.close()


def _git_bytes(root: Path, *args: str) -> bytes | None:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def _changed_files(root: Path, base: str, staged: bool) -> list[tuple[str, bytes, bytes]]:
    """(path, pre at base, post in index or working tree) for every changed file."""
    if _git_bytes(root, "rev-parse", "--is-inside-work-tree") is None:
        raise UsageError(f"{root} is not a git repository")
    if _git_bytes(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") is None:
        raise UsageError(f"unknown --base revision {base!r}")
    listing = ["diff", "--name-only", "-z", "--no-renames", *(["--cached"] if staged else []), base]
    names = set((_git_bytes(root, *listing) or b"").decode().split("\0"))
    if not staged:
        others = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z") or b""
        names |= set(others.decode().split("\0"))
    out = []
    for rel in sorted(n for n in names if n and not n.startswith(".flywire/")):
        pre = _git_bytes(root, "cat-file", "blob", f"{base}:{rel}") or b""
        if staged:
            post = _git_bytes(root, "cat-file", "blob", f":{rel}") or b""
        else:
            path = root / rel
            post = path.read_bytes() if path.is_file() else b""
        out.append((rel, pre, post))
    return out


def _diff_violations(config: Config, base: str, staged: bool) -> list[tuple[str, Verdict]]:
    env = config.envelope
    files = _changed_files(config.root, base, staged)
    protected = gitwildmatch(env.scope.protected_paths)
    found: list[tuple[str, Verdict | None]] = []
    for rel, pre, post in files:
        found.append((rel, scope_guard.protected_path(protected, rel)))
        found.append((rel, evaluate_ast(config.ast_rules, rel, pre, post)))
    deleted = sum(scope_guard.lines_deleted(pre, post) for _, pre, post in files)
    found.append(
        ("(turn)", scope_guard.mass_deletion(deleted, env.scope.max_lines_deleted_per_turn))
    )
    found.append(("(turn)", scope_guard.blast_radius(len(files), env.scope.max_files_modified)))
    disabled = set(env.envelope.disabled_rules)
    return [(rel, v) for rel, v in found if v is not None and v.rule_id not in disabled]


@app.command()
def check(
    diff: Annotated[bool, typer.Option("--diff", help="Check modified files in git.")] = False,
    staged: Annotated[
        bool, typer.Option("--staged", help="Check the index, not the tree.")
    ] = False,
    base: Annotated[str, typer.Option("--base", help="Revision to diff against.")] = "HEAD",
) -> None:
    """Evaluate changed files against Scope + AST rules (pre-commit / CI)."""
    if not diff:
        raise UsageError("nothing to check: pass --diff")
    violations = _diff_violations(_workspace(), base, staged)
    for rel, verdict in violations:
        print(f"{rel}: {verdict.feedback()}")
    if violations:
        print(f"flywire check: {len(violations)} violation(s)")
        raise typer.Exit(EXIT_VIOLATIONS)
    print("flywire check: all clear")


def _events(root: Path, limit: int, *, interventions_only: bool) -> list[EventRow] | None:
    path = root / ".flywire" / "flight_log.db"
    if not path.exists():
        return None
    db = FlightDB(path)
    try:
        return db.recent_events(limit, interventions_only=interventions_only)
    finally:
        db.close()


def _table(title: str, rows: list[EventRow]) -> Table:
    table = Table(title=title)
    for col in ("time", "session", "step", "tool", "op", "target", "verdict", "rule", "ms"):
        table.add_column(col)
    for r in reversed(rows):
        table.add_row(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts)),
            r.session_id[:8],
            str(r.step_idx),
            r.tool_name,
            r.op_class,
            r.target_path or "",
            r.verdict,
            r.rule_id or "",
            f"{r.latency_ms:.2f}",
        )
    return table


@app.command()
def status() -> None:
    """Show active config, packs, rule counts, and recent interventions."""
    config = _workspace()
    env = config.envelope.envelope
    console = Console()
    console.print(f"flywire {VERSION}  workspace: {config.root}")
    console.print(f"mode: {env.mode}  emit_tape: {str(env.emit_tape).lower()}")
    n_ast, n_shell = len(config.ast_rules), len(config.shell_rules)
    console.print(f"rules: {n_ast} AST, {n_shell} shell ({n_ast + n_shell} total)")
    packs = [f"{p} (loaded)" for p in config.packs_loaded]
    packs += [f"{p} (not installed)" for p in config.packs_missing]
    console.print("packs: " + (", ".join(packs) if packs else "none"))
    rows = _events(config.root, 10, interventions_only=True)
    if rows:
        console.print(_table("Recent interventions", rows))
    else:
        console.print("no interventions recorded")


@app.command()
def log(
    tail: Annotated[int, typer.Option(help="Number of recent events.", min=1)] = 20,
) -> None:
    """Show the most recent tool events from .flywire/flight_log.db."""
    rows = _events(find_root(Path.cwd()), tail, interventions_only=False)
    if rows is None:
        print("no flight log yet (.flywire/flight_log.db)")
        return
    Console().print(_table(f"Last {len(rows)} tool events", rows))
