"""The adapter connection: send/recv over TCP, a silent or closed link, probe."""
import asyncio

import pytest

from aioebus.link import connect, probe
from aioebus.transport import RECEIVED, SYN, encode_symbol


async def _server(behaviour):
    """A local TCP server running ``behaviour(reader, writer)`` per client."""
    async def handle(reader, writer):
        try:
            await behaviour(reader, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_link_sends_receives_and_detects_eof():
    async def echo_once(reader, writer):
        writer.write(await reader.read(10))
        await writer.drain()

    async def inner():
        server, port = await _server(echo_once)
        async with server:
            link = await connect("127.0.0.1", port)
            await link.send(b"\x01\x02")
            assert await link.recv() == b"\x01\x02"
            with pytest.raises(ConnectionResetError):
                await link.recv()  # the adapter closed the stream
            link.close()

    asyncio.run(inner())


def test_silent_link_times_out():
    async def silent(reader, writer):
        await asyncio.sleep(1)

    async def inner():
        server, port = await _server(silent)
        async with server:
            link = await connect("127.0.0.1", port, recv_timeout=0.05)
            with pytest.raises(TimeoutError):
                await link.recv()
            link.close()

    asyncio.run(inner())


def test_probe_tells_a_live_adapter_from_a_silent_one():
    async def streaming(reader, writer):
        writer.write(encode_symbol(RECEIVED, SYN))
        await writer.drain()
        await asyncio.sleep(0.5)

    async def silent(reader, writer):
        await asyncio.sleep(0.5)

    async def inner():
        live, live_port = await _server(streaming)
        quiet, quiet_port = await _server(silent)
        async with live, quiet:
            assert await probe("127.0.0.1", live_port, timeout=0.3) is True
            assert await probe("127.0.0.1", quiet_port, timeout=0.1) is False
        with pytest.raises(OSError):
            await probe("127.0.0.1", live_port, timeout=0.3)  # nobody listens now

    asyncio.run(inner())
