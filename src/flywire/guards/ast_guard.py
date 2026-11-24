"""tree-sitter >= 0.25 pre/post AST capture-count guard (contract §1.1)."""

from __future__ import annotations

import sys
import warnings
from collections import OrderedDict
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


@dataclass(frozen=True, eq=False)  # identity hash: used as a cache key
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


@dataclass
class _Parsed:
    root: Node | None
    has_error: bool
    counts: dict[Query, dict[str, int]]


# Agent loops re-submit the previous post as the next pre; memoizing parse + counts by
# source bytes turns the pre side into a cache hit. Bounded LRU (bytes keys, ~8 files).
_CACHE: OrderedDict[tuple[str, bytes], _Parsed] = OrderedDict()
_CACHE_SIZE = 8
# One combined query per (grammar, applicable rule set): a single tree walk instead of one
# per rule. Bounded by the number of distinct path-pattern combinations in the config.
_COMBINED: dict[tuple[str, tuple[CompiledAstRule, ...]], Query] = {}


def _parse(grammar: str, source: bytes) -> _Parsed:
    """Empty source is an empty file: no root, no error."""
    key = (grammar, source)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    root = PARSERS[grammar].parse(source).root_node if source else None
    parsed = _Parsed(root, root.has_error if root is not None else False, {})
    _CACHE[key] = parsed
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return parsed


def prefix_captures(query: str, prefix: str) -> str:
    """Rename every `@capture` (outside string literals) to `@<prefix><capture>`."""
    out: list[str] = []
    i, in_string = 0, False
    while i < len(query):
        c = query[i]
        out.append(c)
        if in_string and c == "\\":
            out.append(query[i + 1 : i + 2])
            i += 1
        elif c == '"':
            in_string = not in_string
        elif c == "@" and not in_string:
            out.append(prefix)
        i += 1
    return "".join(out)


def _combined(grammar: str, rules: tuple[CompiledAstRule, ...]) -> Query:
    key = (grammar, rules)
    query = _COMBINED.get(key)
    if query is None:
        source = "\n".join(prefix_captures(r.rule.query, f"r{i}__") for i, r in enumerate(rules))
        query = _COMBINED[key] = Query(LANG_REGISTRY[grammar], source)
    return query


def _counts(query: Query, parsed: _Parsed) -> dict[str, int]:
    if query not in parsed.counts:
        root = parsed.root
        caps = {} if root is None else QueryCursor(query).captures(root)
        parsed.counts[query] = {name: len(nodes) for name, nodes in caps.items()}
    return parsed.counts[query]


def _applicable(
    rules: Iterable[CompiledAstRule], rel_path: str
) -> tuple[str | None, list[CompiledAstRule]]:
    grammar = EXT_TO_LANG.get(PurePosixPath(rel_path).suffix)
    if grammar is None:
        return None, []
    return grammar, [
        r
        for r in rules
        if grammar in r.queries
        and r.include.match_file(rel_path)
        and not r.exclude.match_file(rel_path)
    ]


def is_guarded(rules: Iterable[CompiledAstRule], rel_path: str) -> bool:
    """Whether any AST rule applies to `rel_path`."""
    return bool(_applicable(rules, rel_path)[1])


def evaluate_ast(
    rules: Iterable[CompiledAstRule], rel_path: str, pre: bytes, post: bytes
) -> Verdict | None:
    """First violated rule (load order) for this pre->post edit, else None."""
    grammar, applicable = _applicable(rules, rel_path)
    if grammar is None or not applicable:
        return None
    pre_p = _parse(grammar, pre)
    post_p = _parse(grammar, post)
    if (
        pre_p.has_error or post_p.has_error
    ):  # D3: never block on a file tree-sitter cannot parse cleanly
        print(
            f"warning: {rel_path} does not parse cleanly; skipping {len(applicable)} AST rule(s)",
            file=sys.stderr,
        )
        return None
    query = _combined(grammar, tuple(applicable))
    pre_counts, post_counts = _counts(query, pre_p), _counts(query, post_p)
    for i, compiled in enumerate(applicable):
        rule = compiled.rule
        name = f"r{i}__{rule.capture}"
        pre_n, post_n = pre_counts.get(name, 0), post_counts.get(name, 0)
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
