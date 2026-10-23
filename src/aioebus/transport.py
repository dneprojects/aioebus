"""Enhanced-adapter transport (Phase 3b) -- the host<->adapter byte protocol.

The enhanced adapter (ebusd's `enh:` device) does bus arbitration and timing in
hardware and speaks a small framed protocol over TCP/serial. For passive
listening we only decode the adapter->host direction and collect the received
eBUS symbols into telegrams, split on SYN. We never send -- so this touches the
bus not at all.

Wire framing (adapter->host):
  * a plain byte < 0x80 is a received eBUS byte (short form)
  * a byte >= 0x80 starts a two-byte symbol ``11ccccdd 10dddddd``; ``cccc`` is
    the 4-bit code, the data byte is ``(dd << 6) | dddddd``
Code 0x1 = RECEIVED (a bus byte); 0xAA among received bytes is SYN, the frame
separator. Other codes (RESETTED/STARTED/INFO/FAILED/errors) are surfaced as
events but ignored by the passive listener.

NOTE: the enhanced adapter allows a single client. On the reference system ebusd
holds it, so live listening needs ebusd stopped first (a maintenance window).
The protocol logic below is validated offline by round-tripping real telegrams
through a synthetic enh stream (tests/test_transport.py).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from .frame import SYN, FrameError, parse_telegram
from .messages import MessageDef
from .passive import MessageRegistry

# adapter -> host codes (cccc)
RESETTED = 0x0
RECEIVED = 0x1
STARTED = 0x2
INFO = 0x3
FAILED = 0xA
ERROR_EBUS = 0xB
ERROR_HOST = 0xC

Event = tuple[int, int]  # (code, data)


def encode_symbol(code: int, data: int) -> bytes:
    """Encode one (code, data) symbol as the adapter/host would put it on the
    wire. Used by tests to build a synthetic stream; the short form is only
    valid for RECEIVED/SEND data below 0x80."""
    if code in (RECEIVED, 0x1) and data < 0x80:
        return bytes((data,))
    first = 0xC0 | ((code & 0x0F) << 2) | ((data >> 6) & 0x03)
    second = 0x80 | (data & 0x3F)
    return bytes((first, second))


def encode_received_frame(raw: bytes) -> bytes:
    """Build the enh byte stream an adapter would emit for one on-wire telegram:
    a leading and trailing SYN with the frame's bytes received in between."""
    out = bytearray(encode_symbol(RECEIVED, SYN))
    for b in raw:
        out += encode_symbol(RECEIVED, b)
    out += encode_symbol(RECEIVED, SYN)
    return bytes(out)


class EnhDecoder:
    """Stateful decoder from raw adapter bytes to (code, data) events."""

    def __init__(self) -> None:
        self._first: int | None = None

    def feed(self, chunk: bytes) -> list[Event]:
        events: list[Event] = []
        for b in chunk:
            if self._first is None:
                if b < 0x80:
                    events.append((RECEIVED, b))      # short-form received byte
                elif b & 0x40:                         # 11ccccdd -> first half
                    self._first = b
                # a lone 10...... second half here is a desync; drop it
            else:
                if (b & 0xC0) == 0x80:                  # 10dddddd -> second half
                    code = (self._first >> 2) & 0x0F
                    data = ((self._first & 0x03) << 6) | (b & 0x3F)
                    events.append((code, data))
                    self._first = None
                elif b & 0x40:                          # resync: new first half
                    self._first = b
                else:
                    self._first = None
        return events


class PassiveListener:
    """Feeds adapter bytes, assembles telegrams on SYN, decodes known ones."""

    def __init__(self, registry: MessageRegistry) -> None:
        self.registry = registry
        self._dec = EnhDecoder()
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[MessageDef, dict]]:
        out: list[tuple[MessageDef, dict]] = []
        for code, data in self._dec.feed(chunk):
            if code != RECEIVED:
                continue
            if data == SYN:
                if self._buf:
                    decoded = self._finish(bytes(self._buf))
                    if decoded is not None:
                        out.append(decoded)
                    self._buf.clear()
            else:
                self._buf.append(data)
        return out

    def _finish(self, raw: bytes) -> tuple[MessageDef, dict] | None:
        try:
            tg = parse_telegram(raw)
        except FrameError:
            return None
        if not tg.crc_ok:
            return None
        return self.registry.decode(tg)


async def listen(
    host: str, port: int, registry: MessageRegistry
) -> AsyncIterator[tuple[MessageDef, dict]]:
    """Connect to an enhanced adapter and yield decoded messages as they arrive.
    Read-only: nothing is ever written to the adapter or the bus."""
    reader, writer = await asyncio.open_connection(host, port)
    listener = PassiveListener(registry)
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            for result in listener.feed(chunk):
                yield result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
