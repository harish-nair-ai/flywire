"""Async bidirectional MCP stdio proxy over newline-delimited JSON-RPC 2.0 (spec §7)."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from asyncio.subprocess import PIPE, Process
from contextlib import suppress

from flywire.engine import EnvelopeEngine
from flywire.proxy.jsonrpc import batch_rejection, parse_tool_call, steer_response
from flywire.proxy.session import ProxySession
from flywire.store.tape import TapeWriter

# ponytail: one JSON-RPC frame must fit in memory to be parsed; 64 MiB per line.
LINE_LIMIT = 64 * 1024 * 1024
GRACE_S = 2.0
EXIT_MAX_HARD_BLOCKS = 42


def _loads(line: bytes) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


async def _stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    return reader, asyncio.StreamWriter(transport, protocol, None, loop)


async def _terminate(proc: Process) -> None:
    """terminate(), GRACE_S grace, then kill(); always reaps (no zombie)."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), GRACE_S)
    except ProcessLookupError:
        pass
    except TimeoutError:
        proc.kill()
    await proc.wait()


async def run_stdio_proxy(
    cmd: list[str], engine: EnvelopeEngine, tape: TapeWriter | None, max_hard_blocks: int
) -> int:
    """Exit code: 0 clean EOF/signal, 42 max consecutive hard blocks, else the child's code.

    Raises FileNotFoundError/PermissionError if the upstream command cannot be started.
    """
    session = ProxySession(engine, tape)
    reader, writer = await _stdio()
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=PIPE, stdout=PIPE, limit=LINE_LIMIT)
    assert proc.stdin is not None and proc.stdout is not None
    child_in, child_out = proc.stdin, proc.stdout

    async def to_client(data: bytes) -> None:
        writer.write(data)
        await writer.drain()  # backpressure: wait while the agent is not reading

    async def client_to_upstream() -> int | None:
        """0 on stdin EOF, 42 on max hard blocks, None if the upstream went away."""
        while line := await reader.readline():
            msg = _loads(line)
            if (rejection := batch_rejection(msg)) is not None:
                await to_client(rejection)
                continue
            request = parse_tool_call(msg)
            pending = session.intercept(request) if request is not None else None
            # non-tool frames (initialize, tools/list, notifications, ...) pass untouched
            if pending is None or pending.call.verdict.action == "ALLOW":
                try:
                    child_in.write(line)
                    await child_in.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return None
                continue
            assert isinstance(msg, dict)
            await to_client(steer_response(msg["id"], pending.call.verdict))
            await session.tape_steered(pending)
            if engine.consecutive_hard_blocks >= max_hard_blocks:
                return EXIT_MAX_HARD_BLOCKS
        return 0

    async def upstream_to_client() -> None:
        while line := await child_out.readline():
            done = session.on_response(_loads(line)) if session.pending else None
            await to_client(line)  # raw bytes, unmodified
            if done is not None:
                await session.tape_allowed(*done)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    client = asyncio.create_task(client_to_upstream())
    upstream = asyncio.create_task(upstream_to_client())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({client, upstream, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            return 0
        code = client.result() if client.done() else None
        if code == 0:  # stdin EOF: let in-flight responses drain, then stop the child
            child_in.close()
            await asyncio.wait({upstream}, timeout=GRACE_S)
        if code is None:  # the upstream exited (or closed its pipes) on its own
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), GRACE_S)
            await _terminate(proc)
            assert proc.returncode is not None
            return proc.returncode
        return code
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        for task in (client, upstream, stopper):
            task.cancel()
        await asyncio.gather(client, upstream, stopper, return_exceptions=True)
        await _terminate(proc)
        print(session.summary(), file=sys.stderr, flush=True)
