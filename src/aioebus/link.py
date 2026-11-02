"""The TCP connection to an enhanced eBUS adapter.

A live bus streams SYN symbols continuously (the bus master's auto-SYN), so a
connection that delivers nothing for a while is dead (adapter rebooted, half-open
TCP) even though the socket still looks open: :meth:`Link.recv` times out then.
"""
from __future__ import annotations

import asyncio
from typing import Final

from .transport import RECEIVED, EnhDecoder

CONNECT_TIMEOUT: Final = 10.0  # s to open the connection
RECV_TIMEOUT: Final = 30.0  # s without a single byte before the link counts as dead
PROBE_TIMEOUT: Final = 6.0  # s for the first eBUS symbol when probing


class Link:
    """One open adapter connection: ``send``/``recv`` are what the engine pumps."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 recv_timeout: float = RECV_TIMEOUT) -> None:
        self._reader = reader
        self._writer = writer
        self._recv_timeout = recv_timeout

    async def send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self) -> bytes:
        """The next bytes from the adapter. Raises TimeoutError on a silent link
        and ConnectionResetError at EOF (read() would return b"" forever)."""
        async with asyncio.timeout(self._recv_timeout):
            data = await self._reader.read(4096)
        if not data:
            raise ConnectionResetError("adapter closed the connection")
        return data

    def close(self) -> None:
        self._writer.close()


async def connect(host: str, port: int, *, timeout: float = CONNECT_TIMEOUT,
                  recv_timeout: float = RECV_TIMEOUT) -> Link:
    """Open the adapter connection (OSError/TimeoutError when unreachable)."""
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection(host, port)
    return Link(reader, writer, recv_timeout)


async def probe(host: str, port: int, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether the adapter streams eBUS symbols: a TCP connect alone is not
    enough, an adapter another client (e.g. ebusd) holds stays silent. Raises
    OSError/TimeoutError when it cannot be reached at all."""
    link = await connect(host, port, timeout=timeout, recv_timeout=timeout)
    decoder = EnhDecoder()
    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await link.recv()
                if any(code == RECEIVED for code, _ in decoder.feed(chunk)):
                    return True
    except (TimeoutError, ConnectionResetError):
        return False  # reachable, but silent or closed at once
    finally:
        link.close()
