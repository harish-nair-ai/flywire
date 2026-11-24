"""ScopeGuard: protected paths, mass deletion, blast radius (spec §5.5)."""

from __future__ import annotations

from collections import Counter

import pathspec

from flywire.models import Verdict


def _steer(rule_id: str, reason: str, remediation: str) -> Verdict:
    return Verdict(
        action="STEER",
        rule_id=rule_id,
        engine="scope_guard",
        reason=reason,
        remediation=remediation,
    )


def path_traversal(raw_path: str) -> Verdict:
    return Verdict(
        action="HARD_BLOCK",
        rule_id="scope-path-traversal",
        engine="scope_guard",
        reason=f"Path '{raw_path}' resolves outside the workspace root.",
        remediation="Only read and modify files inside the workspace root.",
    )


def protected_path(spec: pathspec.PathSpec[pathspec.Pattern], rel_path: str) -> Verdict | None:
    if not spec.match_file(rel_path):
        return None
    return _steer(
        "scope-protected-path",
        f"'{rel_path}' matches a protected path pattern.",
        "Do not modify protected paths; ask a human to make this change.",
    )


def lines_deleted(pre: bytes, post: bytes) -> int:
    """Lines of `pre` with no counterpart in `post` (a moved line is not a deletion)."""
    return sum((Counter(pre.splitlines()) - Counter(post.splitlines())).values())


def mass_deletion(deleted: int, limit: int) -> Verdict | None:
    if deleted <= limit:
        return None
    return _steer(
        "scope-mass-deletion",
        f"This change deletes {deleted} lines (limit {limit} per turn).",
        "Make smaller, targeted edits instead of deleting large blocks of code.",
    )


def blast_radius(distinct_files: int, limit: int) -> Verdict | None:
    if distinct_files <= limit:
        return None
    return _steer(
        "scope-blast-radius",
        f"This change modifies {distinct_files} distinct files (limit {limit}).",
        "Finish and verify the current change before touching more files.",
    )


def unverifiable_edit(rel_path: str) -> Verdict:
    return _steer(
        "scope-unverifiable-edit",
        f"flywire cannot reconstruct the result of this call on '{rel_path}', "
        "a file guarded by AST rules.",
        "Send the complete new file content (e.g. write_file), or an edit whose old text "
        "matches the file exactly, so the change can be verified.",
    )
