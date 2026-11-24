"""Minimal real MCP stdio server used by the proxy tests (spawned as a subprocess).

Every received line is appended verbatim to $MOCK_LOG; every sent line to $MOCK_OUT.
Tools: write_file(path, content), run_command(command), crash(code).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def send(frame: dict[str, object]) -> None:
    line = json.dumps(frame) + "\n"
    with open(os.environ["MOCK_OUT"], "a") as out:
        out.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()


def handle(msg: dict[str, object]) -> None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock", "version": "1.0"},
                },
            }
        )
    elif method == "tools/list":
        tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ("write_file", "run_command")]
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
    elif method == "tools/call":
        params = msg["params"]
        assert isinstance(params, dict)
        name, args = params["name"], params.get("arguments", {})
        if name == "write_file":
            path = Path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "ok"}]}})
        elif name == "run_command":
            proc = subprocess.run(args["command"], shell=True, capture_output=True, text=True)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "content": [{"type": "text", "text": proc.stdout + proc.stderr}],
                        "structuredContent": {"exit_code": proc.returncode},
                        "isError": proc.returncode != 0,
                    },
                }
            )
        elif name == "crash":
            sys.exit(int(args.get("code", 3)))
    # notifications and unknown methods: no response


def main() -> None:
    Path(os.environ["MOCK_PID"]).write_text(str(os.getpid()))
    for line in sys.stdin:
        with open(os.environ["MOCK_LOG"], "a") as log:
            log.write(line)
        handle(json.loads(line))


if __name__ == "__main__":
    main()
