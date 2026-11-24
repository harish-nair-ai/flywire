"""Minimal real Streamable-HTTP MCP upstream (Starlette + uvicorn), spawned by tests.

Usage: python mock_http_server.py <port>. Received POST bodies are appended to $MOCK_LOG.
POST: initialize -> JSON; tools/call -> SSE `message` event. GET: SSE stream that emits an
event, sleeps 1s, then emits a second event (used to prove chunk-by-chunk streaming).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def sse(frame: dict[str, object]) -> bytes:
    return f"event: message\ndata: {json.dumps(frame)}\n\n".encode()


async def mcp(request: Request) -> Response:
    if request.method == "GET":

        async def ticks() -> AsyncIterator[bytes]:
            yield sse({"jsonrpc": "2.0", "method": "notifications/tick", "params": {"n": 1}})
            await asyncio.sleep(1.0)
            yield sse({"jsonrpc": "2.0", "method": "notifications/tick", "params": {"n": 2}})

        return StreamingResponse(ticks(), media_type="text/event-stream")

    body = await request.body()
    with open(os.environ["MOCK_LOG"], "ab") as log:
        log.write(body + b"\n")
    msg = json.loads(body)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "h"}}
        return JSONResponse(
            {"jsonrpc": "2.0", "id": mid, "result": result}, headers={"mcp-session-id": "s-1"}
        )
    if msg.get("method") == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if name == "write_file":
            Path(args["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(args["path"]).write_text(args["content"])
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            proc = subprocess.run(args["command"], shell=True, capture_output=True, text=True)
            result = {
                "content": [{"type": "text", "text": proc.stdout + proc.stderr}],
                "structuredContent": {"exit_code": proc.returncode},
            }
        frame = {"jsonrpc": "2.0", "id": mid, "result": result}
        return StreamingResponse(iter([sse(frame)]), media_type="text/event-stream")
    return Response(status_code=202)


app = Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
