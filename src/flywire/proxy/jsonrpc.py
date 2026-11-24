"""MCP JSON-RPC 2.0 helpers shared by the stdio/HTTP proxies (contract §4)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from flywire.models import ToolCallRequest, ToolResult, Verdict

EXIT_RE = re.compile(r"(?i)exit(?:\s*code)?\s*[:=]?\s*(-?\d+)")
PASSED_RE = re.compile(r"(\d+) passed")
VOLATILE = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b\d{10}(?:\.\d+)?\b"),  # epoch seconds
    re.compile(r"line \d+"),
    re.compile(r":\d+:"),
    re.compile(r"(?:/private)?/(?:tmp|var/folders)/\S*"),
]


def error_hash(text: str) -> str:
    """sha256 of the error text with timestamps, line numbers, and temp paths stripped."""
    for pattern in VOLATILE:
        text = pattern.sub("", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_tool_call(msg: object) -> ToolCallRequest | None:
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None
    try:
        return ToolCallRequest.model_validate(msg)
    except ValidationError:
        return None  # not a valid tools/call: the upstream rejects it too


def steer_response(request_id: Any, verdict: Verdict) -> bytes:
    """Synthesized isError result; `request_id` is echoed with its original JSON type."""
    frame = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"isError": True, "content": [{"type": "text", "text": verdict.feedback()}]},
    }
    return json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"


def result_text(msg: dict[str, Any]) -> str:
    error = msg.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    result = msg.get("result")
    content = result.get("content", []) if isinstance(result, dict) else []
    return "\n".join(
        str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"
    )


def extract_tool_result(msg: dict[str, Any]) -> ToolResult:
    """D9: exit code / passed count / error hash from a tools/call response frame."""
    text = result_text(msg)
    result = msg.get("result")
    if "error" in msg or not isinstance(result, dict):
        exit_code = 1
    else:
        structured = result.get("structuredContent")
        codes = [
            structured.get(k)
            for k in ("exit_code", "exitCode", "returncode")
            if isinstance(structured, dict)
        ]
        found = next((c for c in codes if isinstance(c, int) and not isinstance(c, bool)), None)
        if found is not None:
            exit_code = found
        elif m := EXIT_RE.search(text):
            exit_code = int(m.group(1))
        else:
            exit_code = 1 if result.get("isError") else 0
    passed = sum(int(n) for n in PASSED_RE.findall(text))
    return ToolResult(
        exit_code=exit_code,
        passed_count=passed,
        error_hash=error_hash(text) if exit_code != 0 else None,
    )
