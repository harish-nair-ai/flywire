"""tree-sitter >= 0.25 pre/post AST capture-count guard (contract §1.1)."""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import pathspec
import tree_sitter_python as ts_python
import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Parser, Query

from flywire.models import AstRule

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
    include: pathspec.PathSpec
    exclude: pathspec.PathSpec


def gitwildmatch(patterns: tuple[str, ...]) -> pathspec.PathSpec:
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
    return CompiledAstRule(rule, queries, gitwildmatch(rule.paths), gitwildmatch(rule.exclude_paths))
