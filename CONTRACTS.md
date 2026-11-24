# SHARED_CONTRACTS.md — v0.1 (LOCKED)

Canonical cross-package interfaces for `flywire`, `agent-rr`, `antibody`.
The three packages have **no Python dependency on each other**; they interoperate
only through the files/formats below. Where this file and a PDF spec disagree,
**this file wins**. Spec deviations are marked `DEVIATION (Dn)` with the reason.

Specs (local, not committed): `specs/{flywire,agent_rr,antibody}_implementation_spec.pdf` (+ `.txt`).

---

## 0. Global conventions

| Item | Contract |
|---|---|
| Python | `>=3.11`, `src/` layout, hatchling, `uv`. |
| Models | All external boundaries parse into pydantic v2 models with `model_config = ConfigDict(frozen=True, extra="forbid")`. |
| Paths | All stored/compared paths are workspace-relative POSIX strings (`PurePosixPath`). Traversal check: `resolved.is_relative_to(root.resolve())`. |
| Workspace root discovery | Walk upward from cwd; first dir containing `envelope.toml` **or** `.git` wins; else cwd. Identical in flywire and antibody. |
| Atomic writes | Write to `tempfile.NamedTemporaryFile(dir=<target dir>, delete=False)`, `flush`+`fsync`, then `os.replace`. Applies to `envelope.toml`, `AGENTS.md`, `CLAUDE.md`, `antibody.lock`, generated packs/tests. |
| SQLite connections | Every connection runs: `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`. Parameterized queries only. On close of a writer: `PRAGMA wal_checkpoint(TRUNCATE)` then `close()` so no `-wal`/`-shm` sidecar survives (portable single file). |
| CLI | Typer. Every CLI supports `--version` → prints `<dist-name> 0.1.0` to stdout, exit 0. `--help` on every command, exit 0. `pretty_exceptions_enable=False`. |
| Errors | Users never see tracebacks. Known errors → `error: <actionable message>` on **stderr** + documented exit code. Unexpected exceptions → `error: internal: <ExcType>: <msg>` on stderr, exit 1. |
| Determinism | Chaos: isolated `random.Random(seed)` only (never module-level `random`). Hashes: SHA-256 hex lowercase. JSON written with `sort_keys=True, separators=(",", ":")` when hashed. |
| Ids | Rule id regex: `^[a-z0-9][a-z0-9-]*$`. Pack id: `<namespace>/<name>`, each part matching the same regex. `tape_id`, `session_id`: `uuid4().hex`. |

### 0.1 Exit codes (all packages)

| Pkg | Code | Meaning |
|---|---|---|
| all | 0 | success |
| all | 1 | unexpected internal error; `agent-rr diff`: tapes diverged |
| all | 2 | bad args / invalid config / not found / already exists (`flywire init`) / not installed (`antibody remove`) / missing tape / invalid step |
| agent-rr | 3 | bisect inconclusive |
| flywire | 10 | `check` found violation(s) |
| antibody | 11 | lint/test gate failure |
| antibody | 12 | hash mismatch / lockfile drift / tamper |
| antibody | 14 | distill failed |
| flywire | 42 | `wrap`: `max_hard_blocks` consecutive HARD_BLOCKs reached |
| agent-rr | child | `record`/`fork` return the child command's exit code |

---

## 1. Rule TOML format (consumed by flywire; produced by antibody packs)

Used identically in: flywire `builtin_rules/*.toml`, pack `rules/*.toml`, and local
rules inside `envelope.toml`. **DEVIATION (D1):** specs never name the TOML keys; locked here.

```toml
[[ast_rules]]
id = "py-no-deleted-assertions"
language = "python"                 # "python" | "typescript" | "tsx"
paths = ["tests/**/*.py", "**/*_test.py", "**/test_*.py"]   # gitwildmatch, required, non-empty
exclude_paths = []                  # default []
check = "node_count_decreased"      # "node_count_decreased" | "new_node_introduced" | "node_count_increased"
query = '''[(assert_statement) @target (call function: (attribute attribute: (identifier) @fn (#match? @fn "^assert"))) @target]'''
capture = "target"                  # default "target"
severity = "steer"                  # "steer" | "hard_block"
message = "Test assertions were removed from a test file."
remediation = "Restore the deleted assertions and fix the code under test instead."

[[shell_rules]]
id = "sh-no-hard-reset"
pattern = '^git\s+(reset\s+.*--hard|clean\s+.*-[a-zA-Z]*f[a-zA-Z]*d|clean\s+.*-[a-zA-Z]*d[a-zA-Z]*f)'   # Python `re`, re.search
severity = "hard_block"
message = "Destructive git reset/clean detected."
remediation = "Use `git stash` or targeted `git checkout -- <file>` instead."
```

- A file may contain any number of `[[ast_rules]]` and `[[shell_rules]]`; both keys optional. No other top-level keys in rule files.
- **Pack rule files** (`<pack>/rules/<rule_id with - → _>.toml`) contain **exactly one** rule.
- Invalid TOML, schema errors, or a query that fails `tree_sitter.Query(lang, query)` → config error (flywire exit 2; antibody gate failure exit 11).

### 1.1 AST rule semantics (MUST be bit-identical in flywire and antibody)

**DEVIATION (D2) language scope:** `language="typescript"` applies to `.ts` **and** `.tsx` files; `language="tsx"` applies to `.tsx` only; `python` → `.py`. The grammar is chosen by **file extension** (`.py`→python, `.ts`→typescript, `.tsx`→tsx), so a `typescript` rule's query is compiled against both the typescript and tsx grammars at load time (both must compile).

A rule applies to `rel_path` iff: extension matches the language scope **and**
`PathSpec.from_lines("gitwildmatch", paths).match_file(rel_path)` **and not** exclude spec match.

```
def count_captures(parser, query, source: bytes, capture) -> (int, bool):
    if not source: return 0, False
    tree = parser.parse(source)
    if tree.root_node.has_error: return 0, True
    return len(QueryCursor(query).captures(tree.root_node).get(capture, [])), False
```
- tree-sitter `>=0.25` API only: `Language(...)`, `Parser(lang)`, `Query(lang, src)`, `QueryCursor(query).captures(node)`. Never `Language.query()` / `Query.captures()`. Languages, parsers, and compiled queries are cached once per process.
- **DEVIATION (D3):** if `pre_err or post_err` → skip the rule (warning to stderr), never block. (Spec only checked `post_err`; a pre-broken legacy file would otherwise count `pre=0` and false-positive on legacy debt, violating Legacy Code Immunity.)
- `node_count_decreased`: violation iff `post < pre`. `new_node_introduced` / `node_count_increased`: violation iff `post > pre`.
- File deletion = `post_bytes = b""` (count 0, no error).

### 1.2 Shell rule semantics (MUST be bit-identical in flywire and antibody)

**DEVIATION (D4):** spec splits on `|` before matching, which makes `sh-no-pipe-to-shell` (`curl … | sh`) unmatchable. Locked algorithm — `shell_units(cmd) -> list[str]`:
1. Scan `cmd` left→right tracking single quotes, double quotes, and backslash escapes. Outside single quotes, extract every `$( … )` (nesting-aware) and every `` `…` `` body; each body is recursively processed by `shell_units` and its units appended.
2. At top level (outside quotes/subshells) split into **pipelines** on `&&`, `||`, `;`, `&` (single, not part of `&&`/`>&`/`&>`), and newline.
3. Each pipeline is further split on top-level single `|` (not `||`) into **simple commands**.
4. Units = every pipeline **and** every simple command (pipelines with a single command appear once). Each unit is `strip()`ped with internal whitespace runs collapsed to one space; empty units dropped. Quotes are **kept** (no unquoting).
5. A shell rule fires iff `re.search(pattern, unit)` for any unit. First firing rule in load order wins.

---

## 2. Built-in rule IDs (ownership)

flywire built-ins (`src/flywire/builtin_rules/`): python_core.toml (5), ts_core.toml (5), shell_core.toml (6):
`py-no-deleted-assertions, py-no-bare-except-pass, py-no-ssl-verify-false, py-no-shell-true, py-no-lint-suppression,
ts-no-deleted-expects, ts-no-skipped-tests, ts-no-ts-ignore, ts-no-explicit-any, ts-no-dangerously-set-html,
sh-no-force-push, sh-no-skip-hooks, sh-no-hard-reset, sh-no-destructive-rm, sh-no-pipe-to-shell, sh-no-env-exfil`.

antibody starter packs (18): `core/python-safety` (7 = the 5 py above + `py-no-sql-fstring`, `py-no-eval-exec`),
`core/typescript-safety` (6 = the 5 ts above + `ts-no-empty-catch`), `core/git-hygiene` (5 = sh-* above minus `sh-no-env-exfil`).
Pack rules with the same id as a built-in **override** it (later source wins), so semantics must match the built-in or be strictly better.

Engine-generated rule ids (flywire, not TOML): `scope-path-traversal` (HARD_BLOCK), `scope-protected-path`, `scope-mass-deletion`, `scope-blast-radius`, `scope-unverifiable-edit` (a mutating call on an AST-guarded file whose post-bytes cannot be reconstructed), `stall-loop-detected`, `victory-unverified-commit` (all STEER by default).

**DEVIATION (D5) `sh-no-skip-hooks`:** match `--no-verify` on `git commit`/`git push`, and `-n` (incl. bundled short flags like `-nm`) on `git commit` only. `git push -n` is `--dry-run`, not hook-skipping.
**`sh-no-force-push`:** blocks `git push` with `--force`, `-f`, or `--force-with-lease` (incl. `+refspec`) when the unit names `main` or `master` (implicit current branch is out of scope for v0.1).

---

## 3. `envelope.toml` (flywire reads; antibody edits `envelope.packs` with tomlkit)

Every key optional; defaults below are the canonical values. Unknown keys → config error (exit 2).

```toml
[envelope]
version = "0.1"
mode = "steer"                  # "audit" | "steer" | "strict"
max_hard_blocks = 5             # consecutive HARD_BLOCKs → wrap exits 42
packs = []                      # ["core/python-safety", ...]  (flywire init writes the spec example list)
disabled_rules = []
emit_tape = false
use_builtin_rules = true        # DEVIATION (D6): spec says "built-in core rules (unless disabled)" but gives no switch

[scope]
max_files_modified = 15
max_lines_deleted_per_turn = 150
protected_paths = [".github/workflows/**", "migrations/**", "*.lock", ".env*"]

[stall_detector]
enabled = true
window_size = 5
max_semantic_repeats = 3
max_consecutive_tool_errors = 3
detect_ab_oscillation = true
steer_message = "Repair loop detected. Stop editing, read the full error output, and state a new root-cause hypothesis before modifying code."

[victory_gate]
enabled = true
test_command_patterns = ["^pytest\\b", "^uv run pytest\\b", "^npm test\\b", "^vitest\\b", "^cargo test\\b"]
block_commit_without_pass = true
require_clean_check_before_commit = true

# Local rules: same [[ast_rules]] / [[shell_rules]] arrays as §1, at top level.
```

- Load order: built-ins (if `use_builtin_rules`) → each pack in `packs` order (files `sorted()` within `.flywire/packs/<ns>/<name>/rules/*.toml`) → local rules. Same id: later replaces earlier. Then `disabled_rules` removed.
- **DEVIATION (D7):** a pack listed in `packs` but not installed → one-line warning to stderr, skipped (not an error), so `flywire init` alone works.
- **DEVIATION (D8) `require_clean_check_before_commit`:** when true, `git commit` is STEERed (`victory-unverified-commit`) if the session has any non-ALLOW verdict with `ts > last_test_pass_ts`.
- antibody: if `envelope.toml` absent, create minimal `[envelope]\nversion = "0.1"\npacks = ["<id>"]\n`. Append only if not present; `remove` deletes only that string. Comments/formatting preserved (tomlkit).

---

## 4. flywire steering protocol (MCP JSON-RPC 2.0, newline-delimited UTF-8)

- Non-`tools/call` frames (`initialize`, `tools/list`, `resources/*`, `notifications/*`, responses) pass through **byte-for-byte**.
- `tools/call` params: `{"name": <tool_name>, "arguments": {...}}`.
- STEER / HARD_BLOCK response (upstream NOT invoked), `id` echoed with original JSON type:
```json
{"jsonrpc":"2.0","id":<original id>,"result":{"isError":true,"content":[{"type":"text","text":"[FLYWIRE: <rule_id>] <reason>\nAction required: <remediation>"}]}}
```
- Text is exactly `f"[FLYWIRE: {rule_id}] {reason}\nAction required: {remediation}"` where reason = rule `message`, remediation = rule `remediation`. Regex for consumers: `^\[FLYWIRE: ([a-z0-9-]+)\] `.
- Modes: `audit` → STEER/HARD_BLOCK logged, returned as ALLOW. `strict` → STEER upgraded to HARD_BLOCK. Consecutive HARD_BLOCK counter resets on any ALLOW.
- **DEVIATION (D9) tool result extraction** (for SHELL_EXEC results; needed by stall/victory): exit code = first of `result.structuredContent.{exit_code|exitCode|returncode}` (int); else regex `(?i)exit(?:\s*code)?\s*[:=]?\s*(-?\d+)` over concatenated `result.content[*].text`; else `1 if result.isError else 0`. JSON-RPC `error` → exit 1. Passed-test count = sum of `(\d+) passed` matches in text (0 if none). Error hash = sha256 of text with ISO/epoch timestamps, `line \d+`/`:\d+:` numbers, and `/tmp/…`/`/var/folders/…` paths stripped.

---

## 5. `.flywire/` layout and `flight_log.db`

```
<root>/envelope.toml
<root>/antibody.lock
<root>/AGENTS.md
<root>/.flywire/.gitignore        # written by `flywire init`: "flight_log.db*\ntapes/\n.tmp/\n"
<root>/.flywire/flight_log.db
<root>/.flywire/tapes/<session_id>.tape
<root>/.flywire/packs/<ns>/<name>/...   # exact copy of registry pack dir
<root>/.flywire/.tmp/                    # antibody staging; always emptied in finally
```
`flight_log.db` DDL: exactly flywire spec §6 (`sessions`, `tool_events`, `idx_events_session_step`, `idx_events_session_path`) plus PRAGMAs from §0. `tool_events.verdict ∈ {ALLOW, STEER, HARD_BLOCK}` (the **raw** engine verdict before audit downgrade). antibody `distill --from-flight-db` reads `tool_events WHERE verdict != 'ALLOW'` (`args_json` = JSON of tool arguments, `target_path`, `rule_id`, `reason`).

---

## 6. `.tape` format (agent-rr owns; flywire and antibody read/write)

Single SQLite file. PRAGMAs from §0. `schema_version = '0.1'`. **DEVIATION (D10):** spec DDL is truncated at a page break (`exit code` typo, missing `)`, `chaos_events` undefined, no indexes). Complete DDL:

```sql
CREATE TABLE IF NOT EXISTS tape_meta (
    tape_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '0.1',
    created_at REAL NOT NULL,
    command_argv TEXT NOT NULL,          -- JSON array
    repo_root TEXT NOT NULL,             -- absolute path at record time
    base_commit_sha TEXT NOT NULL,       -- 40-hex `git rev-parse HEAD`
    parent_tape_id TEXT,
    fork_step_idx INTEGER,
    chaos_seed INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'   -- {"producer": "agent-rr"|"flywire", ...}
);
CREATE TABLE IF NOT EXISTS steps (
    step_idx INTEGER PRIMARY KEY,        -- 0,1,2,... contiguous
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llm_call','tool_call','chaos_fault')),
    pre_tree_sha TEXT NOT NULL,          -- 40-hex git tree SHA, present in repo object DB
    post_tree_sha TEXT NOT NULL,
    diff_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error','steered','injected_fault'))
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    provider TEXT NOT NULL,              -- 'openai' | 'anthropic' | 'generic'
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    raw_sse_chunks TEXT,                 -- JSON array of raw SSE event strings, NULL if non-streaming
    http_status INTEGER NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    latency_ms REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,        -- JSON object (redacted)
    result_text TEXT NOT NULL,           -- for steered steps: the exact [FLYWIRE: ...] text
    is_error INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER
);
CREATE TABLE IF NOT EXISTS chaos_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_idx INTEGER NOT NULL REFERENCES steps(step_idx),
    preset TEXT NOT NULL,                -- rate-limit-429 | truncate-stdout | flaky-test | tool-timeout | permission-denied
    seed INTEGER NOT NULL,
    target TEXT NOT NULL,                -- 'llm' | 'tool'
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_step ON llm_calls(step_idx);
CREATE INDEX IF NOT EXISTS idx_tool_calls_step ON tool_calls(step_idx);
CREATE INDEX IF NOT EXISTS idx_chaos_events_step ON chaos_events(step_idx);
```

Rules:
- Exactly one `tape_meta` row. Tapes are append-only; fork creates a **new** tape with `parent_tape_id`, `fork_step_idx`.
- **DEVIATION (D11) multi-writer:** agent-rr's proxy and flywire may write the same tape (`AGENT_RR_TAPE_PATH`). Every step insert runs in `BEGIN IMMEDIATE`, allocates `step_idx = COALESCE(MAX(step_idx), -1) + 1`, and inserts the `steps` row and its child row(s) in the same transaction. If `tape_meta` is empty, the first writer inserts it.
- `diff_text` = git-style unified diff (`diff --git a/<p> b/<p>`, `--- a/<p>` / `--- /dev/null`, `+++ b/<p>`, `@@` hunks). agent-rr uses `git diff-tree -p pre post`.
- **DEVIATION (D12) steered steps:** when a tool call is STEERed/HARD_BLOCKed, nothing is applied so `post_tree_sha == pre_tree_sha`, `status='steered'`, and `diff_text` holds the **rejected proposed diff** (from shadow `pre_bytes`→`post_bytes`). Without this the failure evidence is lost and `antibody distill` has nothing to distill.
- Redaction before insert (both writers): headers `authorization, x-api-key, api-key, cookie, set-cookie, proxy-authorization` (case-insensitive) → `"<REDACTED_HEADER>"`; body/args/result regexes, applied in this order → `"<REDACTED_SECRET>"`: `sk-ant-[A-Za-z0-9_-]{20,}`, `sk-[A-Za-z0-9_-]{20,}`, `gh[pousr]_[A-Za-z0-9]{30,}`, `AKIA[0-9A-Z]{16}`, `(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}`.

### 6.1 Tree snapshot algorithm (agent-rr and flywire `emit_tape`)
```
tmp_idx = mkstemp(dir=tempfile.gettempdir()); os.close; os.unlink   # fresh index
env = {**os.environ, "GIT_INDEX_FILE": tmp_idx}
git read-tree HEAD                                  (env)
git add -A -- . ':!.agent-rr' ':!.flywire'          (env)
[T] git rm --cached -q --ignore-unmatch -- <tape pathspecs>   (env)
sha = git write-tree                                (env)  -> 40-hex
finally: unlink tmp_idx (and tmp_idx + ".lock" if present)
```
`[T]`: only when the tape being written lies inside the repo; pathspecs `':(literal)<rel>'` for `<rel>` and `<rel>-wal`/`-shm`/`-journal` (the tape must never snapshot itself; an exclude pathspec would make `git add` fail when the tape path is gitignored). Never touches `.git/index`, HEAD, or refs. Repo with no commits → actionable error (`error: repository has no commits; make an initial commit first`, exit 2).

### 6.2 flywire tape emission (`emit_tape = true`)
- Tape path: `$AGENT_RR_TAPE_PATH` if set, else `<root>/.flywire/tapes/<session_id>.tape`. Meta (if new): `command_argv` = wrapped upstream argv, `metadata_json={"producer":"flywire","session_id":...}`.
- One `kind='tool_call'` step per `tools/call`. Snapshot work happens **after** the verdict (blocked: after the steer response is flushed; allowed: after upstream response) — never inside `EnvelopeEngine.evaluate()` and never counted in the latency SLA.
- ALLOW → `status='ok'` (or `'error'` if upstream `isError`), diff via `git diff-tree -p`. STEER/HARD_BLOCK → D12.
- Not a git repo / no commits → warning to stderr once, tape emission disabled.

### 6.3 Fork semantics (agent-rr)
- `fork <tape> --at-step N`: worktree restored to `steps[N].pre_tree_sha` (state **before** step N). `fork_llm_idx = count(llm_calls with step_idx < N)`; LLM requests with index `< fork_llm_idx` are served from the parent tape (0 upstream calls); index `>= fork_llm_idx` go live with overrides. Child cmd runs with `cwd=<worktree>`. **DEVIATION (D13):** each mocked request's canonical-JSON SHA-256 (with `model`/`system` overrides not yet applied) is compared to the recorded one; mismatch → warning to stderr (still served), so silent divergence is visible.
- Worktrees: `tempfile.mkdtemp(prefix="agent-rr-")`, `git worktree add --detach`, `git read-tree --reset -u <tree>`, `git clean -fd`; cleanup in `finally` **and** on SIGINT/SIGTERM: `git worktree remove --force` then `git worktree prune`. Zero leftover worktrees/dirs after any exit.

### 6.4 Chaos (agent-rr)
**DEVIATION (D14):** 4 of 5 presets act on tool output, but agent-rr only proxies LLM HTTP. Tool-side presets apply through `agent_rr`'s `@tape.record` tool wrapper (the only tool-execution hook agent-rr owns); `rate-limit-429` applies in the LLM proxy. `chaos.toml`:
```toml
seed = 42
[[faults]]
preset = "rate-limit-429"        # one of the 5 presets
target = "llm"                   # "llm" | "tool"
at_steps = [1]                   # explicit indices (llm index for target=llm, tool-call index for target=tool); OR
probability = 0.0                # 0..1, drawn from Random(seed) once per eligible call, in call order
tool_glob = "*"                  # tool_name fnmatch (tool target only)
path_glob = "*"                  # permission-denied only
delay_s = 0.5                    # tool-timeout only
```
`--chaos <preset>` without a file ≡ one fault with that preset, `probability = 0.3`. Same seed + same call sequence ⇒ byte-identical `chaos_events`.

### 6.5 Bisect (agent-rr)
- **DEVIATION (D15):** majority threshold = `ceil(2*K/3)` (1/1, 2/3, 4/5). Spec's `⌈K×0.67⌉` gives 3/3 for K=3, contradicting its own example.
- **DEVIATION (D16):** CLI adds `-- <agent_cmd...>` (spec says "execute the forked agent run" but gives no command). Each trial: worktree at commit C, overlay `steps[N].pre_tree_sha` file state except `--harness-paths`, run agent cmd then `--check` (cwd = worktree). Endpoint pre-validation is **not** counted as an "evaluation" (acceptance: ≤ 4 evaluations for 8 commits).

### 6.6 Frozen tests (agent-rr `freeze`)
**DEVIATION (D17):** a test that only checks the tape it was generated from is tautological. Generated test resolves tape as: `$AGENT_RR_TAPE_PATH` if set (the run under test), else the reference tape copied next to the test as `<test_stem>.tape` (freeze copies it, checkpointed; frozen test dirs must not be gitignored for `*.tape` if CI needs them — documented in the generated header). Imports only `pytest`, `pathlib`, `os`, `agent_rr.testing`.

---

## 7. antibody pack format

```
<pack-root>/
├── pack.toml
├── rules/<rule_id_snake>.toml                 # exactly one rule (§1)
├── patches/agents_md.md                       # optional; Markdown bullet lines
└── evals/
    ├── test_vaccines.py                       # optional
    └── fixtures/<rule_id>/
        ├── positive_before<ext>  positive_after<ext>     # AST rules; <ext> = suffix of target_sample_path
        ├── negative_before<ext>  negative_after<ext>
        ├── positive.sh                                    # shell rules (DEVIATION D18): whole file stripped = 1 command; MUST fire
        └── negative.sh                                    #   MUST NOT fire
```

`pack.toml`:
```toml
[pack]
namespace = "core"
name = "python-safety"
version = "0.1.0"                    # semver X.Y.Z
description = "..."
authors = ["flywire-maintainers"]
license = "MIT"
languages = ["python"]               # subset of python|typescript|tsx|shell
categories = ["test-tampering"]
flywire_min_version = "0.1.0"

[[vaccines]]
rule_id = "py-no-deleted-assertions"
rule_file = "rules/py_no_deleted_assertions.toml"
fixture_dir = "evals/fixtures/py-no-deleted-assertions"
target_sample_path = "tests/test_sample.py"   # required for AST rules; omitted for shell rules
severity = "critical"                # "critical" | "high" | "medium" | "low"  (metadata only; enforcement uses rule severity)
cwe = "CWE-693"                      # optional
summary = "..."
```
All paths inside pack.toml are pack-relative POSIX and must resolve inside the pack (traversal → gate failure). `rule_file` must contain the rule with id `rule_id`.

### 7.1 Lint gates (antibody `lint`/`test`)
1. Schema: `pack.toml` + every `rules/*.toml` parse into models; every rule file referenced by exactly one vaccine.
2. AST compile: every query compiles for every grammar in its language scope (§1.1 D2).
3. Positive Antigen: AST — rule applies to `target_sample_path` (§1.1) **and** evaluating (`positive_before`, `positive_after`) is a violation. Shell — `positive.sh` fires.
4. Negative Control: (`negative_before`, `negative_after`) → no violation; `negative.sh` does not fire. Parse-error skip counts as **failure** in gates 3 and 4 (fixtures must parse cleanly).
5. Latency: 50 iterations of one rule evaluation (parse pre+post + query); median < 5.0 ms and p99 < 15.0 ms.

### 7.2 `evals/test_*.py` convention
Executed **only** by `antibody test` (never on `pull`), via `python -m pytest <pack>/evals -q` in a subprocess. May import only stdlib, `pytest`, `antibody`. For each vaccine: `test_<rule_id_snake>_positive_antigen` and `test_<rule_id_snake>_negative_control`, using `antibody.validator.runner` public functions against the fixture dir resolved via `Path(__file__).parent / "fixtures" / "<rule_id>"`.

### 7.3 Canonical tree hash
1. Walk regular files (no symlinks followed; symlink → error) under pack dir, skipping any path segment `__pycache__`, file `.DS_Store`, suffix `.pyc`.
2. Sort by `rel_path.as_posix().encode("utf-8")`.
3. **DEVIATION (D19) "text file" defined:** bytes that decode as strict UTF-8 **and** contain no `\x00` → replace `b"\r\n"` with `b"\n"`. Others hashed raw.
4. `h.update(rel.encode() + b"\x00" + data + b"\x00")` per file; return `h.hexdigest()`.

### 7.4 Registry `index.json` and `antibody.lock`
- `index.json` exactly as antibody spec §4; `versions[v].files` = sorted list of all hashed rel paths; `sha256` = §7.3 over `path`.
- Registry source: `ANTIBODY_REGISTRY` env or `--registry`; default `bundled://starter_registry`. Accepted: `bundled://starter_registry`, absolute/relative local dir, `file://` dir, `https://…/` (static files: `index.json` + `files`), `git+https://…` / `git+file://…` (shallow clone into cache). Cache: `${XDG_CACHE_HOME:-~/.cache}/antibody/`; network failure → fall back to cache.
- `antibody.lock` (TOML, root): `schema_version="0.1"`, `registry_url`, `[[installed]]` sorted by `id` with `id, version, sha256, install_path, agents_md_patched`.
- Install: copy to `.flywire/.tmp/<random>/`, hash must equal index → else exit 12 (nothing installed); compile all queries → else exit 11; `os.replace` into `.flywire/packs/<ns>/<name>` (replacing any old dir atomically: move old aside, replace, delete old). `.flywire/.tmp` cleaned in `finally`.

### 7.5 AGENTS.md fencing
Markers (exact, each on its own line):
```
<!-- antibody:start pack="<id>" version="<ver>" -->
<lines of patches/agents_md.md, trailing whitespace stripped>
<!-- antibody:end pack="<id>" -->
```
- Target: `AGENTS.md` (created if absent); `CLAUDE.md` additionally patched **only if it already exists**.
- Upsert: existing block for that pack (matched by `pack="<id>"`, any version) replaced in place; else appended, separated by exactly one blank line, file ends with `\n`. Re-run ⇒ byte-identical file. Remove deletes the block plus the one separating blank line it added. Bytes outside markers untouched. Mismatched/duplicated markers → error exit 2, file untouched.

### 7.6 Distill
- Inputs: `--from-tape` (steps with `status IN ('steered','error')`, D12 diffs, and diffs where assertion count decreased or an anti-pattern was introduced), `--from-flight-db` (§5), `--from-diff`, optional `--step N`.
- Full before/after content: `git -C <repo_root> show <pre_tree_sha>:<path>` when repo + object exist; else reconstructed from the diff hunks. Fixture slice = enclosing function/class node ±3 lines **only if it parses cleanly**, else the whole file. Absolute user paths and secrets (§6 regexes) scrubbed.
- Output pack must pass all 5 gates, else exit 14. `--llm`: up to 3 self-repair retries through the linter; still failing → exit 14, no rule written.

---

## 8. Cross-package acceptance (lead-owned, `integration_tests/test_full_loop.py`)
1. `antibody pull core/python-safety --patch-agents-md` twice → identical `envelope.toml`/`AGENTS.md`, `antibody verify` exit 0.
2. `flywire wrap -- python mock_mcp_server.py` with `emit_tape=true`: a `tools/call` removing an assertion from `tests/test_*.py` → `[FLYWIRE: py-no-deleted-assertions]`, upstream never sees it, engine latency < 15 ms, tape has a `steered` step with the proposed diff.
3. `agent-rr fork <tape> --at-step <steered idx> -- <cmd>` → worktree at pre-violation state; `agent-rr freeze` → generated pytest passes; `git worktree list` shows only the main worktree.
4. `antibody distill --from-tape <tape> --out D` → exit 0; `antibody lint D` exit 0.

---

## 9. Packaging (all packages)
- `pyproject.toml` = spec's exact block **plus** (DEVIATION D20 — `uv run` does not install `optional-dependencies`, and spec omits mypy):
  ```toml
  [dependency-groups]
  dev = ["pytest>=8.2.0", "pytest-asyncio>=0.24.0", "ruff>=0.6.0", "mypy>=1.11"]   # + any stub pkgs actually needed
  [tool.ruff]
  line-length = 100
  target-version = "py311"
  [tool.ruff.lint]
  select = ["E", "F", "W", "I", "B", "UP", "SIM", "RUF"]
  [tool.mypy]
  strict = true
  python_version = "3.11"
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  asyncio_mode = "auto"
  ```
- Package data (`builtin_rules/*.toml`, `starter_registry/**`) must ship in the wheel; verify via `uv build` + inspecting the wheel.
- Each package commits its own `uv.lock`.
