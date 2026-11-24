from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import write

from flywire.classifier import PatchError, PathTraversalError, apply_unified_diff, classify
from flywire.models import OperationClass

OP = OperationClass


def test_full_write_existing_and_new(tmp_path: Path) -> None:
    write(tmp_path, "src/a.py", "old\n")
    intent = classify("write_file", {"path": "src/a.py", "content": "new\n"}, tmp_path)
    assert intent.op_class is OP.FILE_WRITE
    assert (intent.target_path, intent.pre_bytes, intent.post_bytes) == ("src/a.py", b"old\n", b"new\n")
    created = classify("create_file", {"file_path": "./pkg/../b.py", "file_text": "x"}, tmp_path)
    assert (created.target_path, created.pre_bytes, created.post_bytes) == ("b.py", b"", b"x")


def test_path_key_priority(tmp_path: Path) -> None:
    args = {"target_file": "z.py", "filename": "y.py", "file_path": "x.py", "content": ""}
    assert classify("w", args, tmp_path).target_path == "x.py"
    absolute = classify("w", {"filepath": str(tmp_path / "abs.py"), "content": ""}, tmp_path)
    assert absolute.target_path == "abs.py"


def test_search_replace(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "x = 1\nx = 1\n")
    one = classify("edit_file", {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}, tmp_path)
    assert one.post_bytes == b"x = 2\nx = 1\n"
    every = classify(
        "str_replace_editor",
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "y", "replace_all": True},
        tmp_path,
    )
    assert every.post_bytes == b"y\ny\n"
    missing = classify("edit", {"path": "a.py", "old_str": "nope", "new_str": "y"}, tmp_path)
    assert missing.op_class is OP.FILE_WRITE and missing.post_bytes is None


DIFF = """\
diff --git a/src/m.py b/src/m.py
--- a/src/m.py
+++ b/src/m.py
@@ -1,3 +1,3 @@
 a
-b
+B
 c
@@ -6,2 +6,3 @@
 f
--- dashed
+g
+h
"""


def test_unified_diff_multi_hunk_and_dash_lines() -> None:
    original = "a\nb\nc\nd\ne\nf\n-- dashed\n"
    assert apply_unified_diff(original, DIFF) == "a\nB\nc\nd\ne\nf\ng\nh\n"


def test_unified_diff_offset_new_file_and_no_newline() -> None:
    shifted = "z\nz\na\nb\nc\nd\ne\nf\n-- dashed\n"
    assert apply_unified_diff(shifted, DIFF) == "z\nz\na\nB\nc\nd\ne\nf\ng\nh\n"
    new_file = "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+x\n+y\n\\ No newline at end of file\n"
    assert apply_unified_diff("", new_file) == "x\ny"
    with pytest.raises(PatchError):
        apply_unified_diff("q\nr\n", DIFF)


def test_patch_classification(tmp_path: Path) -> None:
    write(tmp_path, "src/m.py", "a\nb\nc\nd\ne\nf\n-- dashed\n")
    with_path = classify("apply_patch", {"path": "src/m.py", "patch": DIFF}, tmp_path)
    assert with_path.post_bytes == b"a\nB\nc\nd\ne\nf\ng\nh\n"
    from_header = classify("apply_patch", {"diff": DIFF}, tmp_path)
    assert from_header.target_path == "src/m.py"
    assert from_header.post_bytes == with_path.post_bytes
    two_files = DIFF + "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-x\n+y\n"
    multi = classify("apply_patch", {"path": "src/m.py", "patch": two_files}, tmp_path)
    assert multi.op_class is OP.FILE_WRITE and multi.post_bytes is None


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "a/../../x"])
def test_traversal_raises(tmp_path: Path, path: str) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(PathTraversalError):
        classify("write_file", {"path": path, "content": "x"}, root)


def test_symlink_escape_raises(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    os.symlink(tmp_path, root / "link")
    with pytest.raises(PathTraversalError):
        classify("write_file", {"path": "link/evil.py", "content": "x"}, root)
    with pytest.raises(PathTraversalError):
        classify("apply_patch", {"diff": "--- a/../x\n+++ b/../x\n@@ -0,0 +1 @@\n+x\n"}, root)


def test_read_only_short_circuits_without_touching_disk(tmp_path: Path) -> None:
    intent = classify("read_file", {"path": "../../etc/passwd"}, tmp_path)
    assert intent.op_class is OP.READ_ONLY
    assert intent.target_path is None and intent.pre_bytes is None
    assert classify("tools_list", {}, tmp_path).op_class is OP.READ_ONLY


@pytest.mark.parametrize(
    "tool, args, op, command",
    [
        ("bash", {"command": "ls -la"}, OP.SHELL_EXEC, "ls -la"),
        ("anything", {"cmd": "pytest -q"}, OP.SHELL_EXEC, "pytest -q"),
        ("exec", {"command": ["git", "status"]}, OP.SHELL_EXEC, "git status"),
        ("run_shell_command", {"script": "make"}, OP.SHELL_EXEC, "make"),
        ("bash", {"command": 'git commit -m "x"'}, OP.GIT_MUTATE, 'git commit -m "x"'),
        ("bash", {"command": "cd a && git push origin x"}, OP.GIT_MUTATE, None),
        ("terminal", {"command": "git reset --soft HEAD"}, OP.GIT_MUTATE, None),
        ("bash", {"command": "git log"}, OP.SHELL_EXEC, None),
    ],
)
def test_shell_classification(
    tmp_path: Path, tool: str, args: dict[str, object], op: OperationClass, command: str | None
) -> None:
    intent = classify(tool, args, tmp_path)
    assert intent.op_class is op
    if command is not None:
        assert intent.shell_command == command
