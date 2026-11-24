"""tree-sitter >= 0.25 pre/post AST capture-count guard (contract §1.1)."""

from __future__ import annotations

import sys
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

import pathspec
import tree_sitter_python as ts_python
import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from flywire.models import SEVERITY_ACTION, AstRule, Verdict

LANG_REGISTRY = {
    "python": Language(ts_python.language()),
    "typescript": Language(ts_typescript.language_typescript()),
    "tsx": Language(ts_typescript.language_tsx()),
}
PARSERS = {name: Parser(lang) for name, lang in LANG_REGISTRY.items()}
EXT_TO_LANG = {".py": "python", ".ts": "typescript", ".tsx": "tsx"}
# D2: a `typescript` rule covers .ts and .tsx, so it is compiled for both grammars.
SCOPE_GRAMMARS = {"python": ("python",), "typescript": ("typescript", "tsx"), "tsx": ("tsx",)}


@dataclass(frozen=True)
class CompiledAstRule:
    rule: AstRule
    queries: dict[str, Query]
    include: pathspec.PathSpec[pathspec.Pattern]
    exclude: pathspec.PathSpec[pathspec.Pattern]


def gitwildmatch(patterns: Iterable[str]) -> pathspec.PathSpec[pathspec.Pattern]:
    # The contract pins "gitwildmatch" semantics; pathspec>=1.0 merely deprecates the name.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "GitWildMatchPattern", DeprecationWarning)
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def compile_ast_rule(rule: AstRule) -> CompiledAstRule:
    """Compile once at load time. Raises ValueError on a bad query or capture name."""
    queries: dict[str, Query] = {}
    for grammar in SCOPE_GRAMMARS[rule.language]:
        try:
            query = Query(LANG_REGISTRY[grammar], rule.query)
        except Exception as exc:  # tree_sitter.QueryError (a ValueError) and friends
            raise ValueError(f"rule {rule.id}: invalid {grammar} query: {exc}") from exc
        names = {query.capture_name(i) for i in range(query.capture_count)}
        if rule.capture not in names:
            raise ValueError(f"rule {rule.id}: capture @{rule.capture} not in query")
        queries[grammar] = query
    return CompiledAstRule(
        rule, queries, gitwildmatch(rule.paths), gitwildmatch(rule.exclude_paths)
    )


def _parse(grammar: str, source: bytes) -> tuple[Node | None, bool]:
    """(root, has_error); an empty source is an empty file: no root, no error."""
    if not source:
        return None, False
    root = PARSERS[grammar].parse(source).root_node
    return root, root.has_error


def _count(query: Query, root: Node | None, capture: str) -> int:
    return 0 if root is None else len(QueryCursor(query).captures(root).get(capture, []))


def evaluate_ast(
    rules: Iterable[CompiledAstRule], rel_path: str, pre: bytes, post: bytes
) -> Verdict | None:
    """First violated rule (load order) for this pre->post edit, else None."""
    grammar = EXT_TO_LANG.get(PurePosixPath(rel_path).suffix)
    if grammar is None:
        return None
    applicable = [
        r
        for r in rules
        if grammar in r.queries
        and r.include.match_file(rel_path)
        and not r.exclude.match_file(rel_path)
    ]
    if not applicable:
        return None
    pre_root, pre_err = _parse(grammar, pre)
    post_root, post_err = _parse(grammar, post)
    if pre_err or post_err:  # D3: never block on a file tree-sitter cannot parse cleanly
        print(
            f"warning: {rel_path} does not parse cleanly; skipping {len(applicable)} AST rule(s)",
            file=sys.stderr,
        )
        return None
    for compiled in applicable:
        rule, query = compiled.rule, compiled.queries[grammar]
        pre_n, post_n = (
            _count(query, pre_root, rule.capture),
            _count(query, post_root, rule.capture),
        )
        violated = post_n < pre_n if rule.check == "node_count_decreased" else post_n > pre_n
        if violated:
            return Verdict(
                action=SEVERITY_ACTION[rule.severity],
                rule_id=rule.id,
                engine="ast_guard",
                reason=rule.message,
                remediation=rule.remediation,
            )
    return None
