"""Enhanced-adapter transport -- the host<->adapter byte protocol.

The enhanced adapter (ebusd's `enh:` device) does bus arbitration and timing in
hardware and speaks a small framed protocol over TCP/serial. This module holds both
directions: it decodes the adapter->host stream into (code, data) events and
encodes the host->adapter commands; the engine assembles the received eBUS
symbols into telegrams.

Wire framing (adapter->host):
  * a plain byte < 0x80 is a received eBUS byte (short form)
  * a byte >= 0x80 starts a two-byte symbol ``11ccccdd 10dddddd``; ``cccc`` is
    the 4-bit code, the data byte is ``(dd << 6) | dddddd``
Code 0x1 = RECEIVED (a bus byte); 0xAA among received bytes is SYN, the frame
separator. Other codes (RESETTED/STARTED/INFO/FAILED/errors) are surfaced as
events.

The enhanced adapter allows a single client: while ebusd holds it, nobody else
can listen.
"""
from __future__ import annotations

from .frame import SYN

# adapter -> host codes (cccc)
RESETTED = 0x0
RECEIVED = 0x1
STARTED = 0x2
INFO = 0x3
FAILED = 0xA
ERROR_EBUS = 0xB
ERROR_HOST = 0xC

# host -> adapter codes
SEND = 0x1  # put a byte on the bus
START = 0x2  # arbitrate with the given master address (SYN: withdraw)

Event = tuple[int, int]  # (code, data)


def encode_command(code: int, data: int) -> bytes:
    """A host->adapter symbol, always in the two-byte form: ebusd never
    short-forms SEND/START and the adapter firmware drops short-form sends."""
    return bytes((0xC0 | ((code & 0x0F) << 2) | ((data >> 6) & 0x03), 0x80 | (data & 0x3F)))


def encode_symbol(code: int, data: int) -> bytes:
    """One adapter->host symbol as the adapter puts it on the wire (a received
    byte below 0x80 in the short form), e.g. to feed a recorded or synthetic
    stream to the engine."""
    if code == RECEIVED and data < 0x80:
        return bytes((data,))
    return encode_command(code, data)


def encode_received_frame(raw: bytes) -> bytes:
    """The adapter stream for one on-wire telegram (a capture, a test): a
    leading and trailing SYN with the frame's bytes received in between."""
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
                elif b & 0x80:                          # resync: new first half
                    self._first = b
                else:                                   # short form: still a bus byte
                    events.append((RECEIVED, b))
                    self._first = None
        return events
