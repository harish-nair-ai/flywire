"""Tool intent classifier + in-memory shadow patcher (spec §5.1)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flywire.guards.shell_guard import shell_units
from flywire.models import OperationClass, ToolCallIntent

PATH_KEYS = ("path", "file_path", "filepath", "filename", "target_file")
REPLACE_KEYS = (("old_str", "new_str"), ("old_string", "new_string"))
SHELL_TOOL = re.compile(r"bash|shell|exec|command|terminal|run", re.IGNORECASE)
GIT_MUTATE = re.compile(r"^git\s+(?:commit|push|reset|checkout|rebase)\b")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PathTraversalError(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class PatchError(Exception):
    """The patch does not apply to the current file contents."""


@dataclass
class _Hunk:
    old_start: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


def _header_path(line: str) -> str | None:
    raw = line[4:].rstrip("\n").split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def parse_unified_diff(diff: str) -> dict[str | None, list[_Hunk]]:
    """Hunks per file (`+++` path, or `---` path for deletions; None if headerless)."""
    files: dict[str | None, list[_Hunk]] = {}
    current: str | None = None
    lines = diff.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = _header_path(lines[i + 1]) or _header_path(line)
            i += 2
            continue
        m = HUNK.match(line)
        if not m:
            i += 1
            continue
        old_left = int(m.group(2) or "1")
        new_left = int(m.group(4) or "1")
        hunk = _Hunk(old_start=int(m.group(1)))
        last: list[list[str]] = []
        i += 1
        while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
            body = lines[i]
            tag, text = (body[0], body[1:]) if body not in ("\n", "") else (" ", body)
            if tag == "\\":  # "\ No newline at end of file" applies to the previous line
                for side in last:
                    side[-1] = side[-1].rstrip("\n")
            elif tag == " ":
                hunk.old.append(text)
                hunk.new.append(text)
                old_left, new_left, last = old_left - 1, new_left - 1, [hunk.old, hunk.new]
            elif tag == "-":
                hunk.old.append(text)
                old_left, last = old_left - 1, [hunk.old]
            elif tag == "+":
                hunk.new.append(text)
                new_left, last = new_left - 1, [hunk.new]
            else:
                raise PatchError(f"malformed hunk line: {body!r}")
            i += 1
        files.setdefault(current, []).append(hunk)
    return files


def _apply_hunks(original: str, hunks: list[_Hunk]) -> str:
    src = original.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        size = len(hunk.old)
        expected = max(hunk.old_start - 1, 0) if size else hunk.old_start
        candidates = [expected, *range(cursor, len(src) - size + 1)]
        at = next(
            (c for c in candidates if c >= cursor and src[c : c + size] == hunk.old),
            None,
        )
        if at is None:
            raise PatchError(f"hunk at line {hunk.old_start} does not apply")
        out += src[cursor:at] + hunk.new
        cursor = at + size
    return "".join(out + src[cursor:])


def apply_unified_diff(original: str, diff: str) -> str:
    """Apply a single-file unified diff in memory (exact context, offset search)."""
    files = parse_unified_diff(diff)
    if len(files) != 1:
        raise PatchError(f"expected a single-file diff, got {len(files)} files")
    return _apply_hunks(original, next(iter(files.values())))


def _explicit_command(args: dict[str, Any]) -> str | None:
    for key in ("command", "cmd"):
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return shlex.join(value)
    return None


def _named_shell_command(tool_name: str, args: dict[str, Any]) -> str | None:
    """A shell-named tool (`run_shell_command`) whose only string argument is the command."""
    strings = [v for v in args.values() if isinstance(v, str)]
    return strings[0] if SHELL_TOOL.search(tool_name) and len(strings) == 1 else None


def _shell_intent(tool_name: str, args: dict[str, Any], command: str) -> ToolCallIntent:
    git = any(GIT_MUTATE.match(u) for u in shell_units(command))
    op = OperationClass.GIT_MUTATE if git else OperationClass.SHELL_EXEC
    return ToolCallIntent(op_class=op, tool_name=tool_name, shell_command=command, raw_args=args)


def _diff_text(args: dict[str, Any]) -> str | None:
    value = args.get("patch", args.get("diff"))
    return value if isinstance(value, str) else None


def _is_write(args: dict[str, Any]) -> bool:
    return (
        isinstance(args.get("content"), str)
        or isinstance(args.get("file_text"), str)
        or any(
            isinstance(args.get(o), str) and isinstance(args.get(n), str) for o, n in REPLACE_KEYS
        )
        or _diff_text(args) is not None
    )


def _resolve(root: Path, raw: str) -> tuple[Path, str]:
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise PathTraversalError(raw)
    return resolved, resolved.relative_to(root).as_posix()


def _shadow_post(pre: bytes, args: dict[str, Any], diff: str | None) -> bytes | None:
    """Post-edit bytes, or None when the edit cannot be reconstructed (upstream will fail)."""
    for key in ("content", "file_text"):
        if isinstance(args.get(key), str):
            return str(args[key]).encode("utf-8")
    try:
        text = pre.decode("utf-8")
        for old_key, new_key in REPLACE_KEYS:
            old, new = args.get(old_key), args.get(new_key)
            if isinstance(old, str) and isinstance(new, str):
                if old == "":
                    return new.encode("utf-8") if not text else None
                if old not in text:
                    return None
                return text.replace(old, new, -1 if args.get("replace_all") else 1).encode()
        if diff is not None:
            return apply_unified_diff(text, diff).encode("utf-8")
    except (UnicodeDecodeError, PatchError):
        return None
    return None


def classify(tool_name: str, args: dict[str, Any], root: Path) -> ToolCallIntent:
    """Map a tool call to an intent. `root` must be resolved. Raises PathTraversalError."""
    command = _explicit_command(args)
    if command is not None:
        return _shell_intent(tool_name, args, command)
    if not _is_write(args):
        command = _named_shell_command(tool_name, args)
        if command is not None:
            return _shell_intent(tool_name, args, command)
        # READ_ONLY short-circuit: no path resolution, no disk, no AST work.
        return ToolCallIntent(op_class=OperationClass.READ_ONLY, tool_name=tool_name, raw_args=args)

    raw_path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), None)
    diff = _diff_text(args)
    if raw_path is None and diff is not None:
        try:
            headers = [p for p in parse_unified_diff(diff) if p is not None]
        except PatchError:
            headers = []
        raw_path = headers[0] if len(headers) == 1 else None
    if raw_path is None:  # e.g. a multi-file patch: nothing single-file to shadow
        return ToolCallIntent(
            op_class=OperationClass.FILE_WRITE, tool_name=tool_name, raw_args=args
        )
    path, rel = _resolve(root, raw_path)
    pre = path.read_bytes() if path.is_file() else b""
    return ToolCallIntent(
        op_class=OperationClass.FILE_WRITE,
        tool_name=tool_name,
        target_path=rel,
        pre_bytes=pre,
        post_bytes=_shadow_post(pre, args, diff),
        raw_args=args,
    )
