from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Parser, Query, QueryCursor

from flywire.config import load_config
from flywire.guards.ast_guard import LANG_REGISTRY, CompiledAstRule, evaluate_ast

AUTH_TEST = b"""def test_login():
    assert login("a", "b")
    assert token() is not None
    assert user().name == "a"
"""


@pytest.fixture(scope="module")
def rules(tmp_path_factory: pytest.TempPathFactory) -> tuple[CompiledAstRule, ...]:
    return load_config(tmp_path_factory.mktemp("ws")).ast_rules


def fired(rules: tuple[CompiledAstRule, ...], path: str, pre: bytes, post: bytes) -> str | None:
    verdict = evaluate_ast(rules, path, pre, post)
    return verdict.rule_id if verdict else None


def test_predicates_are_applied_by_query_cursor() -> None:
    """#eq? / #match? must filter captures in the installed tree-sitter, not just parse."""
    lang = LANG_REGISTRY["python"]
    tree = Parser(lang).parse(b"f(verify=False)\nf(other=False)\nx = 1  # noqa\ny = 2  # ok\n")
    eq = Query(lang, '(keyword_argument name: (identifier) @k (#eq? @k "verify")) @t')
    match = Query(lang, '((comment) @t (#match? @t "noqa"))')
    assert [n.text for n in QueryCursor(eq).captures(tree.root_node)["t"]] == [b"verify=False"]
    assert [n.text for n in QueryCursor(match).captures(tree.root_node)["t"]] == [b"# noqa"]


# --- spec §8 acceptance -------------------------------------------------------------


def test_deleting_one_of_three_asserts_steers(rules: tuple[CompiledAstRule, ...]) -> None:
    post = AUTH_TEST.replace(b"    assert token() is not None\n", b"")
    verdict = evaluate_ast(rules, "tests/test_auth.py", AUTH_TEST, post)
    assert verdict is not None
    assert (verdict.action, verdict.rule_id) == ("STEER", "py-no-deleted-assertions")
    assert verdict.engine == "ast_guard"
    assert verdict.reason == "Test assertions were removed from a test file."


def test_legacy_noqa_debt_is_immune(rules: tuple[CompiledAstRule, ...]) -> None:
    legacy = b"import os  # noqa\nimport sys  # noqa\n"
    with_fn = legacy + b"\n\ndef added():\n    return 1\n"
    assert fired(rules, "src/legacy.py", legacy, with_fn) is None
    third = with_fn + b"import re  # noqa\n"
    assert fired(rules, "src/legacy.py", with_fn, third) == "py-no-lint-suppression"


def test_broken_post_syntax_skips_without_crash(
    rules: tuple[CompiledAstRule, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    assert fired(rules, "tests/test_auth.py", AUTH_TEST, b"def test_login(:\n    assert\n") is None
    assert "does not parse" in capsys.readouterr().err


def test_broken_pre_syntax_skips_d3(
    rules: tuple[CompiledAstRule, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    """D3: a pre-broken legacy file must not count pre=0 and false-positive."""
    broken = b"def f(:\n    pass\nimport os  # noqa\n"
    fixed = b"def f():\n    pass\nimport os  # noqa\n"
    assert fired(rules, "src/x.py", broken, fixed) is None
    assert "does not parse" in capsys.readouterr().err


def test_file_deletion_counts_as_zero(rules: tuple[CompiledAstRule, ...]) -> None:
    assert fired(rules, "tests/test_auth.py", AUTH_TEST, b"") == "py-no-deleted-assertions"


# --- every built-in rule: positive and negative fixture -------------------------------

CASES = [
    # (rule, path, pre, post_positive, post_negative)
    (
        "py-no-deleted-assertions",
        "tests/unit/test_calc.py",
        b"class T:\n    def test_a(self):\n        self.assertEqual(1, 1)\n        assert 2\n",
        b"class T:\n    def test_a(self):\n        assert 2\n",
        b"class T:\n    def test_a(self):\n        self.assertEqual(1, 1)\n        assert 2\n"
        b"        assert 3\n",
    ),
    (
        "py-no-bare-except-pass",
        "src/app.py",
        b"def f():\n    return g()\n",
        b"def f():\n    try:\n        return g()\n    except Exception:\n        pass\n",
        b"def f():\n    try:\n        return g()\n    except Exception:\n"
        b"        log()\n        raise\n",
    ),
    (
        "py-no-ssl-verify-false",
        "src/net.py",
        b"r = requests.get(url)\n",
        b"r = requests.get(url, verify=False)\n",
        b"r = requests.get(url, verify=True, timeout=False)\n",
    ),
    (
        "py-no-shell-true",
        "src/run.py",
        b"subprocess.run(['ls'])\n",
        b"subprocess.run('ls', shell=True)\n",
        b"subprocess.run(['ls'], check=True, shell=False)\n",
    ),
    (
        "py-no-lint-suppression",
        "src/mod.py",
        b"x = 1\n",
        b"x = 1  # type: ignore[assignment]\n",
        b"x = 1  # explain why x is one\n",
    ),
    (
        "ts-no-deleted-expects",
        "web/calc.test.ts",
        b"it('adds', () => {\n  expect(add(1, 2)).toBe(3);\n  expect(add(0, 0)).toBe(0);\n});\n",
        b"it('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n",
        b"it('adds', () => {\n  expect(add(1, 2)).toBe(3);\n  expect(add(0, 0)).toBe(0);\n"
        b"  expect(add(2, 2)).toBe(4);\n});\n",
    ),
    (
        "ts-no-skipped-tests",
        "web/calc.spec.tsx",
        b"describe('calc', () => { it('adds', () => {}); });\n",
        b"describe('calc', () => { it.skip('adds', () => {}); });\n",
        b"describe('calc', () => { it.each([1])('adds', () => {}); });\n",
    ),
    (
        "ts-no-ts-ignore",
        "src/util.ts",
        b"const a: number = f();\n",
        b"// @ts-ignore\nconst a: number = f();\n",
        b"// ignore the cache here\nconst a: number = f();\n",
    ),
    (
        "ts-no-explicit-any",
        "src/api/client.ts",
        b"export function parse(x: string): number { return 1; }\n",
        b"export function parse(x: any): number { return 1; }\n",
        b"export function parse(x: unknown): string { return 'a'; }\n",
    ),
    (
        "ts-no-dangerously-set-html",
        "src/View.tsx",
        b"export const V = () => <div className='a'>{text}</div>;\n",
        b"export const V = () => <div dangerouslySetInnerHTML={{ __html: text }} />;\n",
        b"export const V = () => <div title='dangerouslySetInnerHTML'>{text}</div>;\n",
    ),
]


@pytest.mark.parametrize("rule_id, path, pre, positive, negative", CASES, ids=[c[0] for c in CASES])
def test_builtin_rule_positive_and_negative(
    rules: tuple[CompiledAstRule, ...],
    rule_id: str,
    path: str,
    pre: bytes,
    positive: bytes,
    negative: bytes,
) -> None:
    assert fired(rules, path, pre, positive) == rule_id
    assert fired(rules, path, pre, negative) is None


def test_rule_scope_by_extension_and_paths(rules: tuple[CompiledAstRule, ...]) -> None:
    any_pre = b"let x: string = 'a';\n"
    any_post = b"let x: any = 'a';\n"
    assert fired(rules, "src/a.tsx", any_pre, any_post) == "ts-no-explicit-any"  # D2
    assert fired(rules, "lib/a.ts", any_pre, any_post) is None  # outside src/**
    html = b"const v = <div dangerouslySetInnerHTML={{ __html: s }} />;\n"
    assert fired(rules, "src/a.ts", b"", html) is None  # tsx-only rule never applies to .ts
    assert fired(rules, "src/notes.md", b"assert x\n", b"") is None  # unknown extension
    # assertions removed from a non-test python file are fine
    assert fired(rules, "src/calc.py", AUTH_TEST, b"") is None


def test_exclude_paths(tmp_path: Path) -> None:
    (tmp_path / "envelope.toml").write_text(
        "[envelope]\nuse_builtin_rules = false\n"
        "[[ast_rules]]\nid='no-print'\nlanguage='python'\npaths=['**/*.py']\n"
        "exclude_paths=['scripts/**']\ncheck='node_count_increased'\n"
        "query='(call function: (identifier) @f (#eq? @f \"print\")) @target'\n"
        "severity='hard_block'\nmessage='m'\nremediation='r'\n"
    )
    rules = load_config(tmp_path).ast_rules
    verdict = evaluate_ast(rules, "src/a.py", b"", b"print(1)\n")
    assert verdict is not None and verdict.action == "HARD_BLOCK"
    assert evaluate_ast(rules, "scripts/a.py", b"", b"print(1)\n") is None
