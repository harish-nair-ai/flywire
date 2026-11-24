"""Tool intent classifier + in-memory shadow patcher (spec §5.1)."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flywire.guards.shell_guard import shell_units
from flywire.models import FileEdit, OperationClass, ToolCallIntent

PATH_KEYS = ("path", "file_path", "filepath", "filename", "target_file")
REPLACE_KEYS = (("oldText", "newText"), ("old_str", "new_str"), ("old_string", "new_string"))
MOVE_KEYS = ("source", "destination")
SHELL_TOOL = re.compile(r"bash|shell|exec|command|terminal|run", re.IGNORECASE)
# A tool named like this that takes a path mutates it even if flywire knows no content key.
MUTATING_TOOL = re.compile(
    r"write|edit|creat|delet|remov|move|renam|patch|replac|insert|append|apply|updat|save"
    r"|mkdir|unlink|copy|truncat|modif",
    re.IGNORECASE,
)
MKDIR_TOOL = re.compile(r"mkdir|(?:creat|make).*dir", re.IGNORECASE)
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
        or isinstance(args.get("edits"), list)
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


def _read(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def _files(path: Path) -> list[Path]:
    """`path` itself, or every regular file under a directory (symlinks not followed)."""
    if not path.is_dir():
        return [path]
    found = []
    for parent, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        found += [Path(parent, n) for n in sorted(names) if Path(parent, n).is_file()]
    return found


def _replace(text: str, edit: object) -> str | None:
    """One search/replace edit (official `oldText`/`newText`, Claude Code `old_string`/...)."""
    if not isinstance(edit, dict):
        return None
    for old_key, new_key in REPLACE_KEYS:
        old, new = edit.get(old_key), edit.get(new_key)
        if isinstance(old, str) and isinstance(new, str):
            if old_key == "oldText":  # the official filesystem server normalizes CRLF
                text, old, new = (s.replace("\r\n", "\n") for s in (text, old, new))
            if old == "":
                return new if not text else None
            if old not in text:
                return None
            return text.replace(old, new, -1 if edit.get("replace_all") else 1)
    return None


def _shadow_post(pre: bytes, args: dict[str, Any], diff: str | None) -> bytes | None:
    """Post-edit bytes, or None when the edit cannot be reconstructed."""
    for key in ("content", "file_text"):
        if isinstance(args.get(key), str):
            return str(args[key]).encode("utf-8")
    try:
        text = pre.decode("utf-8")
        edits = args.get("edits")
        if isinstance(edits, list):  # applied sequentially, like the servers do
            for edit in edits:
                step = _replace(text, edit)
                if step is None:
                    return None
                text = step
            return text.encode("utf-8")
        replaced = _replace(text, args)
        if replaced is not None:
            return replaced.encode("utf-8")
        if diff is not None:
            return apply_unified_diff(text, diff).encode("utf-8")
    except (UnicodeDecodeError, PatchError):
        return None
    return None


def _write(
    tool_name: str, args: dict[str, Any], edits: list[FileEdit], target: str | None
) -> ToolCallIntent:
    one = edits[0] if len(edits) == 1 and edits[0].target_path == target else None
    return ToolCallIntent(
        op_class=OperationClass.FILE_WRITE,
        tool_name=tool_name,
        target_path=target,
        pre_bytes=one.pre_bytes if one else None,
        post_bytes=one.post_bytes if one else None,
        extra_edits=() if one else tuple(edits),
        raw_args=args,
    )


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _patch_edits(root: Path, diff: str) -> tuple[list[FileEdit], str | None]:
    """Per-file edits of a patch whose `+++`/`---` headers name its files."""
    try:
        files = parse_unified_diff(diff)
    except PatchError:
        return [], None
    if not files or None in files:
        return [], None
    edits = []
    for raw, hunks in files.items():
        assert raw is not None
        path, rel = _resolve(root, raw)
        pre = _read(path)
        try:
            post: bytes | None = _apply_hunks(pre.decode("utf-8"), hunks).encode("utf-8")
        except (UnicodeDecodeError, PatchError):
            post = None
        edits.append(FileEdit(target_path=rel, pre_bytes=pre, post_bytes=post))
    return edits, edits[0].target_path if len(edits) == 1 else None


def _move_edits(root: Path, args: dict[str, Any]) -> tuple[list[FileEdit], str]:
    """A move deletes every source file and creates it (with the same bytes) at the destination."""
    src, src_rel = _resolve(root, args["source"])
    dst, _ = _resolve(root, args["destination"])
    edits = []
    for f in _files(src):
        data, to = _read(f), dst / f.relative_to(src) if f != src else dst
        edits.append(FileEdit(target_path=_rel(root, f), pre_bytes=data, post_bytes=b""))
        edits.append(FileEdit(target_path=_rel(root, to), pre_bytes=_read(to), post_bytes=data))
    return edits, src_rel


def _mutation(tool_name: str, args: dict[str, Any], root: Path) -> ToolCallIntent | None:
    """A mutating-named tool with path argument(s) but no content flywire can shadow."""
    if all(isinstance(args.get(k), str) for k in MOVE_KEYS):
        return _write(tool_name, args, *_move_edits(root, args))
    raw = [args[k] for k in (*PATH_KEYS, *MOVE_KEYS) if isinstance(args.get(k), str)]
    if not raw:
        return None
    resolved = [_resolve(root, r) for r in raw]
    target = resolved[0][1]
    if MKDIR_TOOL.search(tool_name):
        return _write(tool_name, args, [], target)
    edits = [
        FileEdit(target_path=_rel(root, f), pre_bytes=_read(f), post_bytes=None)
        for path, _ in resolved
        for f in _files(path)
    ]
    return _write(tool_name, args, edits, target)


def classify(tool_name: str, args: dict[str, Any], root: Path) -> ToolCallIntent:
    """Map a tool call to an intent. `root` must be resolved. Raises PathTraversalError."""
    command = _explicit_command(args)
    if command is not None:
        return _shell_intent(tool_name, args, command)
    if args.get("dryRun") is True:  # official edit_file preview: nothing is written
        return ToolCallIntent(op_class=OperationClass.READ_ONLY, tool_name=tool_name, raw_args=args)
    if _is_write(args):
        return _write_intent(tool_name, args, root)
    command = _named_shell_command(tool_name, args)
    if command is not None:
        return _shell_intent(tool_name, args, command)
    if MUTATING_TOOL.search(tool_name):
        intent = _mutation(tool_name, args, root)
        if intent is not None:
            return intent
    # READ_ONLY short-circuit: no path resolution, no disk, no AST work.
    return ToolCallIntent(op_class=OperationClass.READ_ONLY, tool_name=tool_name, raw_args=args)


def _write_intent(tool_name: str, args: dict[str, Any], root: Path) -> ToolCallIntent:
    raw_path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), None)
    diff = _diff_text(args)
    if raw_path is None and diff is not None:
        return _write(tool_name, args, *_patch_edits(root, diff))
    if raw_path is None:  # nothing names the file: nothing to shadow
        return _write(tool_name, args, [], None)
    path, rel = _resolve(root, raw_path)
    pre = _read(path)
    edit = FileEdit(target_path=rel, pre_bytes=pre, post_bytes=_shadow_post(pre, args, diff))
    return _write(tool_name, args, [edit], rel)
