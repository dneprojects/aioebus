"""Enhanced-adapter transport -- the host<->adapter byte protocol.

The enhanced adapter (ebusd's `enh:` device) does bus arbitration and timing in
hardware and speaks a small framed protocol over TCP/serial. This module decodes
the adapter->host direction into (code, data) events; the engine assembles the
received eBUS symbols into telegrams and sends the host->adapter symbols.

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
                elif b & 0x80:                          # resync: new first half
                    self._first = b
                else:                                   # short form: still a bus byte
                    events.append((RECEIVED, b))
                    self._first = None
        return events
