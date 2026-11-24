"""Real stdio MCP server mimicking `@modelcontextprotocol/server-filesystem` tool shapes.

edit_file(path, edits=[{oldText, newText}], dryRun): edits applied sequentially, each
replacing the first exact occurrence (line endings normalized); a miss is an error;
dryRun returns the diff without writing. Also write_file, move_file, create_directory,
read_file. Every received line is appended to $MOCK_LOG.
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any


def reply(mid: object, text: str, is_error: bool = False) -> None:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def edit_file(args: dict[str, Any]) -> str:
    path = Path(args["path"])
    original = path.read_text().replace("\r\n", "\n")
    text = original
    for edit in args["edits"]:
        old = edit["oldText"].replace("\r\n", "\n")
        if old not in text:
            raise ValueError(f"Could not find exact match for edit:\n{edit['oldText']}")
        text = text.replace(old, edit["newText"].replace("\r\n", "\n"), 1)
    diff = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True)))
    if not args.get("dryRun"):
        path.write_text(text)
    return f"```diff\n{diff}```"


def call(name: str, args: dict[str, Any]) -> str:
    if name == "edit_file":
        return edit_file(args)
    if name == "write_file":
        Path(args["path"]).write_text(args["content"])
        return f"Successfully wrote to {args['path']}"
    if name == "move_file":
        dest = Path(args["destination"])
        if dest.exists():
            raise ValueError(f"Destination already exists: {dest}")
        Path(args["source"]).rename(dest)
        return f"Successfully moved {args['source']} to {dest}"
    if name == "create_directory":
        Path(args["path"]).mkdir(parents=True, exist_ok=True)
        return f"Successfully created directory {args['path']}"
    if name == "read_file":
        return Path(args["path"]).read_text()
    raise ValueError(f"Unknown tool: {name}")


def main() -> None:
    for line in sys.stdin:
        with open(os.environ["MOCK_LOG"], "a") as log:
            log.write(line)
        msg = json.loads(line)
        mid = msg.get("id")
        if msg.get("method") == "initialize":
            info = {"name": "secure-filesystem-server", "version": "mock"}
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": info}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
            sys.stdout.flush()
        elif msg.get("method") == "tools/call":
            params = msg["params"]
            try:
                reply(mid, call(params["name"], params.get("arguments", {})))
            except (OSError, ValueError, KeyError) as exc:
                reply(mid, f"Error: {exc}", is_error=True)


if __name__ == "__main__":
    main()
