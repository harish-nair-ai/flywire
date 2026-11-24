"""Pydantic v2 boundary models: config, rules, intents, verdicts, JSON-RPC frames."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

VerdictAction = Literal["ALLOW", "STEER", "HARD_BLOCK"]
Mode = Literal["audit", "steer", "strict"]
Severity = Literal["steer", "hard_block"]
RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
PackId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")]
SEVERITY_ACTION: dict[str, VerdictAction] = {"steer": "STEER", "hard_block": "HARD_BLOCK"}


class OperationClass(StrEnum):
    FILE_WRITE = "FILE_WRITE"
    SHELL_EXEC = "SHELL_EXEC"
    GIT_MUTATE = "GIT_MUTATE"
    READ_ONLY = "READ_ONLY"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Verdict(Frozen):
    action: VerdictAction
    rule_id: str | None = None
    engine: str
    reason: str = ""
    remediation: str = ""
    latency_ms: float = 0.0

    def feedback(self) -> str:
        return f"[FLYWIRE: {self.rule_id}] {self.reason}\nAction required: {self.remediation}"


class AstRule(Frozen):
    id: RuleId
    language: Literal["python", "typescript", "tsx"]
    paths: tuple[str, ...] = Field(min_length=1)
    exclude_paths: tuple[str, ...] = ()
    check: Literal["node_count_decreased", "new_node_introduced", "node_count_increased"]
    query: str
    capture: str = "target"
    severity: Severity
    message: str
    remediation: str


class ShellRule(Frozen):
    id: RuleId
    pattern: str
    severity: Severity
    message: str
    remediation: str


class RuleFile(Frozen):
    ast_rules: tuple[AstRule, ...] = ()
    shell_rules: tuple[ShellRule, ...] = ()


class EnvelopeSection(Frozen):
    version: str = "0.1"
    mode: Mode = "steer"
    max_hard_blocks: int = Field(default=5, ge=1)
    packs: tuple[PackId, ...] = ()
    disabled_rules: tuple[str, ...] = ()
    emit_tape: bool = False
    use_builtin_rules: bool = True


class ScopeSection(Frozen):
    max_files_modified: int = Field(default=15, ge=1)
    max_lines_deleted_per_turn: int = Field(default=150, ge=0)
    protected_paths: tuple[str, ...] = (".github/workflows/**", "migrations/**", "*.lock", ".env*")


class StallSection(Frozen):
    enabled: bool = True
    window_size: int = Field(default=5, ge=2)
    max_semantic_repeats: int = Field(default=3, ge=2)
    max_consecutive_tool_errors: int = Field(default=3, ge=1)
    detect_ab_oscillation: bool = True
    steer_message: str = (
        "Repair loop detected. Stop editing, read the full error output, "
        "and state a new root-cause hypothesis before modifying code."
    )


class VictorySection(Frozen):
    enabled: bool = True
    test_command_patterns: tuple[str, ...] = (
        "^pytest\\b",
        "^uv run pytest\\b",
        "^npm test\\b",
        "^vitest\\b",
        "^cargo test\\b",
    )
    block_commit_without_pass: bool = True
    require_clean_check_before_commit: bool = True

    @field_validator("test_command_patterns")
    @classmethod
    def _compiles(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:  # not a ValueError, so pydantic would not wrap it
                raise ValueError(f"invalid regex {p!r}: {exc}") from exc
        return patterns


class EnvelopeFile(Frozen):
    envelope: EnvelopeSection = EnvelopeSection()
    scope: ScopeSection = ScopeSection()
    stall_detector: StallSection = StallSection()
    victory_gate: VictorySection = VictorySection()
    ast_rules: tuple[AstRule, ...] = ()
    shell_rules: tuple[ShellRule, ...] = ()


class FileEdit(Frozen):
    """One file's shadowed change; `post_bytes=None` = the result cannot be reconstructed."""

    target_path: str
    pre_bytes: bytes
    post_bytes: bytes | None


class ToolCallIntent(Frozen):
    op_class: OperationClass
    tool_name: str
    target_path: str | None = None  # the tool's path argument (logged, stall-tracked)
    pre_bytes: bytes | None = None  # set when the call edits exactly `target_path`
    post_bytes: bytes | None = None
    extra_edits: tuple[FileEdit, ...] = ()  # multi-file calls: move, directories, patches
    shell_command: str | None = None
    raw_args: dict[str, Any]

    def file_edits(self) -> tuple[FileEdit, ...]:
        """Every file this call changes, for the guards."""
        if self.target_path is None or self.pre_bytes is None:
            return self.extra_edits
        return (FileEdit(target_path=self.target_path, pre_bytes=self.pre_bytes,
                         post_bytes=self.post_bytes), *self.extra_edits)  # fmt: skip


class ToolResult(Frozen):
    """Outcome of an executed shell/tool call (contract §4 D9)."""

    exit_code: int
    passed_count: int = 0
    error_hash: str | None = None


# JSON-RPC frames ignore unknown keys (e.g. MCP `_meta`): forbidding them would make a
# newer-protocol frame unparseable and therefore unguarded.
class ToolCallParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    name: str
    arguments: dict[str, Any] = {}


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    jsonrpc: Literal["2.0"]
    id: int | str
    method: Literal["tools/call"]
    params: ToolCallParams
