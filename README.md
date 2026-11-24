# flywire

## How it works, and why it's built this way

flywire decides ALLOW, STEER or HARD_BLOCK locally, before a tool call touches disk. No LLM or network call is involved, and measured p99 is about 7 ms against a 15 ms budget.

- It compares ASTs before and after the edit instead of grepping text. tree-sitter (0.25 `Query`/`QueryCursor` API) parses the current file and the shadow-patched result in memory, then compares capture counts. An agent is only blocked for debt it introduces, never for what was already in the file.
- Parsers, languages and queries are compiled once at startup. All 10 AST rules run as one combined query per tree, and parse results are cached by content hash. Read-only calls skip AST and disk work entirely.
- Shell commands are checked per pipeline and per simple command, including `$(...)` and backtick subshells, so `curl … | sh` is caught as well as `rm -rf /`.
- Loops are caught with a rolling window in SQLite (WAL mode, indexed by session and step): the same file edited repeatedly without test progress, A→B→A reverts, and the same error repeating.
- The victory gate keeps two timestamps per session and refuses `git commit` while code is newer than the last passing test.
- The MCP stdio proxy streams newline-delimited JSON-RPC frame by frame and passes every non-`tools/call` method through byte-for-byte, so it works across MCP protocol versions.

Cross-package file formats are documented in `CONTRACTS.md`.
