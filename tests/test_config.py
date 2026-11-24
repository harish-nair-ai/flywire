from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write

from flywire.config import ConfigError, find_root, load_config
from flywire.models import Verdict

BUILTIN_AST = {
    "py-no-deleted-assertions",
    "py-no-bare-except-pass",
    "py-no-ssl-verify-false",
    "py-no-shell-true",
    "py-no-lint-suppression",
    "ts-no-deleted-expects",
    "ts-no-skipped-tests",
    "ts-no-ts-ignore",
    "ts-no-explicit-any",
    "ts-no-dangerously-set-html",
}
BUILTIN_SHELL = [
    "sh-no-force-push",
    "sh-no-skip-hooks",
    "sh-no-hard-reset",
    "sh-no-destructive-rm",
    "sh-no-pipe-to-shell",
    "sh-no-env-exfil",
]

PACK_RULE = """
[[ast_rules]]
id = "{id}"
language = "python"
paths = ["**/*.py"]
check = "new_node_introduced"
query = '(call function: (identifier) @fn (#eq? @fn "eval")) @target'
severity = "hard_block"
message = "{message}"
remediation = "Do not use eval."
"""


def test_defaults_without_envelope_load_all_builtins(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert {r.rule.id for r in cfg.ast_rules} == BUILTIN_AST
    assert [r.rule.id for r in cfg.shell_rules] == BUILTIN_SHELL
    env = cfg.envelope
    assert env.envelope.mode == "steer"
    assert env.envelope.max_hard_blocks == 5
    assert env.envelope.packs == ()
    assert env.envelope.use_builtin_rules is True
    assert env.envelope.emit_tape is False
    assert env.scope.max_files_modified == 15
    assert env.scope.max_lines_deleted_per_turn == 150
    assert env.scope.protected_paths == (".github/workflows/**", "migrations/**", "*.lock", ".env*")
    assert env.stall_detector.window_size == 5
    assert env.stall_detector.max_semantic_repeats == 3
    assert env.stall_detector.steer_message.startswith("Repair loop detected.")
    assert env.victory_gate.test_command_patterns[0] == "^pytest\\b"
    assert env.victory_gate.require_clean_check_before_commit is True


def test_typescript_rules_compile_for_both_grammars(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    by_id = {r.rule.id: r for r in cfg.ast_rules}
    assert set(by_id["ts-no-explicit-any"].queries) == {"typescript", "tsx"}
    assert set(by_id["ts-no-dangerously-set-html"].queries) == {"tsx"}
    assert set(by_id["py-no-shell-true"].queries) == {"python"}


@pytest.mark.parametrize(
    "body, needle",
    [
        ("[envelope\nmode = 'steer'", "envelope.toml"),
        ("[envelope]\nbogus_key = 1\n", "bogus_key"),
        ("[envelope]\nmode = 'yolo'\n", "mode"),
        ("[scope]\nmax_files_modified = 'many'\n", "max_files_modified"),
        (
            "[[ast_rules]]\nid='bad-q'\nlanguage='python'\npaths=['**/*.py']\n"
            "check='new_node_introduced'\nquery='(not_a_node) @target'\n"
            "severity='steer'\nmessage='m'\nremediation='r'\n",
            "bad-q",
        ),
        (
            "[[ast_rules]]\nid='bad-cap'\nlanguage='python'\npaths=['**/*.py']\n"
            "check='new_node_introduced'\nquery='(comment) @other'\n"
            "severity='steer'\nmessage='m'\nremediation='r'\n",
            "bad-cap",
        ),
        (
            "[[ast_rules]]\nid='no-paths'\nlanguage='python'\npaths=[]\n"
            "check='new_node_introduced'\nquery='(comment) @target'\n"
            "severity='steer'\nmessage='m'\nremediation='r'\n",
            "paths",
        ),
        (
            "[[shell_rules]]\nid='bad-re'\npattern='(unclosed'\n"
            "severity='steer'\nmessage='m'\nremediation='r'\n",
            "bad-re",
        ),
        (
            "[[shell_rules]]\nid='Bad_Id'\npattern='x'\nseverity='steer'\nmessage='m'\nremediation='r'\n",
            "id",
        ),
        ("[envelope]\npacks = ['not-a-pack-id']\n", "packs"),
        ("[victory_gate]\ntest_command_patterns = ['(']\n", "test_command_patterns"),
    ],
)
def test_invalid_config_fails_fast(tmp_path: Path, body: str, needle: str) -> None:
    write(tmp_path, "envelope.toml", body)
    with pytest.raises(ConfigError, match=needle):
        load_config(tmp_path)


def test_precedence_builtin_then_pack_then_local(tmp_path: Path) -> None:
    write(
        tmp_path,
        "envelope.toml",
        "[envelope]\npacks = ['core/python-safety']\n"
        + PACK_RULE.format(id="py-no-eval", message="local wins"),
    )
    pack = tmp_path / ".flywire/packs/core/python-safety/rules"
    write(pack.parent, "rules/py_no_eval.toml", PACK_RULE.format(id="py-no-eval", message="pack"))
    write(
        pack.parent,
        "rules/py_no_shell_true.toml",
        PACK_RULE.format(id="py-no-shell-true", message="pack override"),
    )
    cfg = load_config(tmp_path)
    by_id = {r.rule.id: r.rule for r in cfg.ast_rules}
    assert by_id["py-no-eval"].message == "local wins"
    assert by_id["py-no-shell-true"].message == "pack override"
    assert cfg.packs_loaded == ("core/python-safety",)
    assert len(by_id) == 11


def test_pack_rule_file_must_hold_exactly_one_rule(tmp_path: Path) -> None:
    write(tmp_path, "envelope.toml", "[envelope]\npacks = ['core/x']\n")
    two = PACK_RULE.format(id="a-one", message="m") + PACK_RULE.format(id="a-two", message="m")
    write(tmp_path, ".flywire/packs/core/x/rules/a.toml", two)
    with pytest.raises(ConfigError, match="exactly one rule"):
        load_config(tmp_path)


def test_missing_pack_warns_and_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "envelope.toml", "[envelope]\npacks = ['core/python-safety']\n")
    cfg = load_config(tmp_path)
    assert cfg.packs_missing == ("core/python-safety",)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "core/python-safety" in err and "not installed" in err


def test_disabled_rules_and_builtin_switch(tmp_path: Path) -> None:
    write(
        tmp_path,
        "envelope.toml",
        "[envelope]\ndisabled_rules = ['py-no-shell-true', 'sh-no-hard-reset']\n",
    )
    cfg = load_config(tmp_path)
    ids = {r.rule.id for r in cfg.ast_rules} | {r.rule.id for r in cfg.shell_rules}
    assert "py-no-shell-true" not in ids and "sh-no-hard-reset" not in ids
    assert len(ids) == 14

    write(tmp_path, "envelope.toml", "[envelope]\nuse_builtin_rules = false\n")
    cfg = load_config(tmp_path)
    assert cfg.ast_rules == () and cfg.shell_rules == ()


def test_find_root_walks_up_to_envelope_or_git(tmp_path: Path) -> None:
    deep = tmp_path / "a/b/c"
    deep.mkdir(parents=True)
    assert find_root(deep) == deep
    (tmp_path / "a/.git").mkdir()
    assert find_root(deep) == (tmp_path / "a").resolve()
    write(tmp_path, "a/b/envelope.toml", "")
    assert find_root(deep) == (tmp_path / "a/b").resolve()


def test_verdict_feedback_format() -> None:
    v = Verdict(action="STEER", rule_id="py-x", engine="ast_guard", reason="R", remediation="Fix")
    assert v.feedback() == "[FLYWIRE: py-x] R\nAction required: Fix"
    with pytest.raises(ValueError, match="frozen"):
        v.action = "ALLOW"
