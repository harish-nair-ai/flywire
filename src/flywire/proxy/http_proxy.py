"""Starlette + httpx reverse proxy for Streamable HTTP / SSE MCP servers (spec §7)."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from flywire.engine import EnvelopeEngine
from flywire.proxy.jsonrpc import batch_rejection, parse_tool_call, steer_response
from flywire.proxy.session import ProxySession, id_key
from flywire.store.tape import TapeWriter

# Hop-by-hop headers, plus length/encoding: the body is re-chunked and relayed decoded.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_local_request(request: Request) -> bool:
    """MCP spec: validate Host/Origin so a web page can't reach this proxy via DNS rebinding."""
    host = urlsplit("//" + request.headers.get("host", "")).hostname
    origin = request.headers.get("origin")
    return host in LOCAL_HOSTS and (origin is None or urlsplit(origin).hostname in LOCAL_HOSTS)


DROP_HEADERS = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailers", "transfer-encoding", "upgrade", "host", "content-length",
        "content-encoding",
    }
)  # fmt: skip


def _loads(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        return None


class SSEFrames:
    """Incremental SSE parser: yields the JSON `data` payload of each completed event."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[object]:
        self._buf += chunk.replace(b"\r\n", b"\n")
        *events, self._buf = self._buf.split(b"\n\n")
        out = []
        for event in events:
            data = b"\n".join(
                line[5:].removeprefix(b" ")
                for line in event.split(b"\n")
                if line.startswith(b"data:")
            )
            if data:
                out.append(_loads(data))
        return out


async def _relay(
    resp: httpx.Response, session: ProxySession, tracked: str | None
) -> AsyncIterator[bytes]:
    """Relay chunk-by-chunk. For a tracked tools/call the result is recorded before the chunk
    carrying it is released, so the agent's next request sees the updated session state."""
    try:
        if tracked is None:
            async for chunk in resp.aiter_bytes():
                yield chunk
        elif resp.headers.get("content-type", "").startswith("text/event-stream"):
            frames = SSEFrames()
            async for chunk in resp.aiter_bytes():
                done = [d for f in frames.feed(chunk) if (d := session.on_response(f))]
                yield chunk
                for d in done:
                    await session.tape_allowed(*d)
        else:  # a single JSON-RPC response: hold back only its final chunk until recorded
            body, held = bytearray(), b""
            async for chunk in resp.aiter_bytes():
                if held:
                    yield held
                body += chunk
                held = chunk
            final = session.on_response(_loads(bytes(body)))
            if held:
                yield held
            if final:
                await session.tape_allowed(*final)
    finally:
        if tracked is not None:
            session.pending.pop(tracked, None)  # response never carried a result
        await resp.aclose()


def create_app(engine: EnvelopeEngine, upstream: str, tape: TapeWriter | None) -> Starlette:
    session = ProxySession(engine, tape)
    clients: list[httpx.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            clients.append(client)
            yield
        print(session.summary(), file=sys.stderr, flush=True)

    async def proxy(request: Request) -> Response:
        if not _is_local_request(request):
            return Response("forbidden: non-local Host or Origin", status_code=403)
        body = await request.body()
        msg = _loads(body) if request.method == "POST" else None
        if (rejection := batch_rejection(msg)) is not None:
            return Response(rejection, media_type="application/json")
        call = parse_tool_call(msg)
        pending = session.intercept(call) if call is not None else None
        if pending is not None and pending.call.verdict.action != "ALLOW":
            assert isinstance(msg, dict)
            return Response(
                steer_response(msg["id"], pending.call.verdict),
                media_type="application/json",
                background=BackgroundTask(session.tape_steered, pending),
            )
        query = f"?{request.url.query}" if request.url.query else ""
        headers = [
            (k, v)
            for k, v in request.headers.raw
            if k.decode("latin-1").lower() not in DROP_HEADERS
        ]
        upstream_request = clients[0].build_request(
            request.method, upstream.rstrip("/") + request.url.path + query,
            headers=headers, content=body,
        )  # fmt: skip
        resp = await clients[0].send(upstream_request, stream=True)
        return StreamingResponse(
            _relay(resp, session, id_key(pending.request.id) if pending else None),
            status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items() if k.lower() not in DROP_HEADERS},
        )

    methods = ["GET", "POST", "DELETE"]
    return Starlette(routes=[Route("/{path:path}", proxy, methods=methods)], lifespan=lifespan)
