"""Active reading over the enhanced adapter (Phase 4, read side).

Builds the master request and drives the read transaction. The transaction FSM
is ported from ebusd's protocol_direct.cpp (active-get path):

    arbitrate(QQ via START) -> STARTED/FAILED
    send ZZ PB SB NN DD   (each SEND byte is echoed back as RECEIVED, verified)
    send CRC              (echoed, verified)
    recv master ACK (0x00) | NAK (0xff)
    recv slave: SN, SN data bytes, SCRC  (unescaped); verify CRC
    send ACK (0x00)       (echoed)
    send SYN              (releases the bus)

`qq` defaults to 0x31 -- the address ebusd itself uses on the reference bus, so
it is a proven-good master address that is free exactly when ebusd is stopped
(the single-client adapter needs ebusd down anyway). The core `ReadDriver` is a
pure byte-level state machine (testable against a simulated adapter); `read()`
only pumps bytes between it and the socket. Nothing is sent until the caller
explicitly runs a read -- this module never writes on import or construction.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .frame import SYN, crc8, escape, unescape
from .messages import MessageDef
from .scan import SCAN_PBSB, ScanResult, decode_scan
from .transport import FAILED, RECEIVED, STARTED, EnhDecoder

DEFAULT_SOURCE = 0x31  # ebusd's own master address on the reference bus (free when ebusd is down)
SEND = 0x1
START = 0x2
ACK = 0x00
NAK = 0xFF


class ReadError(Exception):
    """An active read failed (arbitration lost, collision, NAK, bad CRC)."""


def _symbol2(code: int, value: int) -> bytes:
    """Encode a host->adapter symbol in the two-byte form. ebusd always uses the
    long form for SEND/START (its EnhancedDevice::send never short-forms), and
    the adapter firmware drops short-form sends, so we match it exactly."""
    return bytes((0xC0 | ((code & 0x0F) << 2) | ((value >> 6) & 0x03),
                  0x80 | (value & 0x3F)))


def build_read_frame(
    zz: int, pbsb: str, msgid: str, qq: int = DEFAULT_SOURCE, master_data: bytes = b""
) -> bytes:
    """Return the escaped on-wire master frame (incl. CRC) for a read request.

    For a plain read the master payload is just the message id; `master_data`
    appends any master-side argument bytes for the rare parametrised reads.
    """
    pb, sb = int(pbsb[0:2], 16), int(pbsb[2:4], 16)
    data = bytes.fromhex(msgid) + master_data
    logical = bytes((qq, zz, pb, sb, len(data))) + data
    crc = crc8(escape(logical))
    return escape(logical) + escape(bytes((crc,)))


def build_read(msg: MessageDef, qq: int = DEFAULT_SOURCE) -> bytes:
    """Build the read frame for a message definition (needs a 2-hex zz)."""
    if not (msg.zz and len(msg.zz) == 2):
        raise ValueError(f"{msg.name}: no destination address to read from")
    return build_read_frame(int(msg.zz, 16), msg.pbsb, msg.msgid, qq)


def build_write_frame(
    zz: int, pbsb: str, master_data: bytes, qq: int = DEFAULT_SOURCE
) -> bytes:
    """Build the on-wire master frame for a write; `master_data` already includes
    the message id and the encoded field bytes (see MessageDef.encode)."""
    return build_read_frame(zz, pbsb, "", qq, master_data)


class _CommandDriver:
    """Pure byte-level FSM shared by active reads and writes: arbitrate (START
    with QQ), send the command bytes (ZZ PB SB NN DD + CRC, each echoed back and
    verified for collision), then handle the master ACK/NAK. Subclasses decide
    what happens once the command is ACKed. No I/O here, so it is fully testable
    against a simulated adapter; `done`/`result`/`error` report the outcome.
    """

    def __init__(self, zz: int, pbsb: str, data: bytes, qq: int = DEFAULT_SOURCE):
        wire = build_read_frame(zz, pbsb, "", qq, data)  # QQ ZZ PB SB NN data CRC
        self.qq = qq
        self._cmd = wire[len(escape(bytes((qq,)))):]  # bytes to SEND after arbitration
        self._dec = EnhDecoder()
        self._state = "arb"
        self._sendpos = 0
        self.done = False
        self.result = None
        self.error: str | None = None

    def start(self) -> list[bytes]:
        """Initial symbols to send: the arbitration START with QQ."""
        return [_symbol2(START, self.qq)]

    def feed(self, chunk: bytes) -> list[bytes]:
        """Process adapter bytes, return the enh symbols to send next."""
        return self.feed_events(self._dec.feed(chunk))

    def feed_events(self, events) -> list[bytes]:
        """Process already-decoded (code, data) events. Used by the engine, which
        owns the single stream decoder and routes events to the active driver."""
        out: list[bytes] = []
        for code, data in events:
            if self.done:
                break
            out += self._step(code, data)
        return out

    def _fail(self, msg: str) -> list[bytes]:
        self.error = msg
        self.done = True
        return []

    def _step(self, code: int, data: int) -> list[bytes]:
        if self._state == "arb":
            if code == STARTED:
                self._state = "cmd"
                return self._send_next_cmd()
            if code == FAILED:
                return self._fail(f"arbitration lost to {data:#04x}")
            return []  # ignore SYN/other while waiting to win

        if self._state == "cmd":  # verify echo of the byte we just sent
            if code != RECEIVED or data != self._cmd[self._sendpos - 1]:
                return self._fail("collision: echo mismatch while sending command")
            return self._send_next_cmd()

        if self._state == "ack":  # master ACK/NAK of our command
            if code == RECEIVED and data == ACK:
                return self._acked()
            if code == RECEIVED and data == NAK:
                return self._fail("command NAKed by slave")
            return self._fail("unexpected symbol waiting for command ACK")

        return self._handle(code, data)

    def _send_next_cmd(self) -> list[bytes]:
        if self._sendpos >= len(self._cmd):
            self._state = "ack"
            return []
        b = self._cmd[self._sendpos]
        self._sendpos += 1
        return [_symbol2(SEND, b)]

    def _acked(self) -> list[bytes]:  # subclass: command accepted by the slave
        raise NotImplementedError

    def _handle(self, code: int, data: int) -> list[bytes]:  # subclass: states past ack
        return []


class ReadDriver(_CommandDriver):
    """Active read: after the command is ACKed, collect and verify the slave's
    response frame (SN + data + CRC), then ACK it and release the bus."""

    def __init__(self, zz: int, pbsb: str, msgid: str, qq: int = DEFAULT_SOURCE,
                 master_data: bytes = b""):
        super().__init__(zz, pbsb, bytes.fromhex(msgid) + master_data, qq)
        self._slave_wire = bytearray()

    def _acked(self) -> list[bytes]:
        self._state = "res"
        return []

    def _handle(self, code: int, data: int) -> list[bytes]:
        if self._state != "res" or code != RECEIVED:
            return []
        self._slave_wire.append(data)
        u = unescape(bytes(self._slave_wire))
        if len(u) >= 1 and len(u) >= u[0] + 2:  # SN + SN data + CRC
            return self._finish_slave(u)
        return []

    def _finish_slave(self, u: bytes) -> list[bytes]:
        sn = u[0]
        sdata = bytes(u[1:1 + sn])
        scrc = u[1 + sn]
        if crc8(escape(bytes((sn,)) + sdata)) != scrc:
            return self._fail("slave CRC mismatch")
        self.result = sdata
        self.done = True
        # ACK the good answer, then release the bus with a SYN
        return [_symbol2(SEND, ACK), _symbol2(SEND, SYN)]


class WriteDriver(_CommandDriver):
    """Active write: the slave ACKs the command (there is no response frame), so
    once ACKed the write is done; we release the bus with a SYN."""

    def __init__(self, zz: int, pbsb: str, master_data: bytes, qq: int = DEFAULT_SOURCE):
        super().__init__(zz, pbsb, master_data, qq)  # master_data already includes the id

    def _acked(self) -> list[bytes]:
        self.result = True
        self.done = True
        return [_symbol2(SEND, SYN)]


async def _run_with_retry(send, recv, make_driver, timeout: float, tries: int):
    """Run a command driver to completion, retrying only when the shared bus was
    not actually acquired (lost arbitration / collision before we owned it).
    Returns the finished driver; a NAK or other error is returned, not retried,
    and is never retried for a write because the slave may already have applied
    one copy. The bus acquire itself is safe to retry: an incomplete master frame
    is rejected on CRC, so no half-write lands."""
    last = "no attempt"
    for _ in range(tries):
        drv = make_driver()
        for sym in drv.start():
            await send(sym)
        try:
            while not drv.done:
                chunk = await asyncio.wait_for(recv(), timeout)
                for sym in drv.feed(chunk):
                    await send(sym)
        except TimeoutError:
            last = "timeout"
            continue
        if drv.error is None:
            return drv
        last = drv.error
        if "arbitration lost" in last or "collision" in last:
            await asyncio.sleep(0.05)  # let the bus reach a fresh SYN
            continue
        break
    raise ReadError(last)


class ActiveReader:
    """Async active reader over an enh link. `send`/`recv` are the only I/O: on a
    socket, `send(bytes)` writes and `recv()` awaits the next chunk from the
    adapter. Read-only in the sense that it only ever issues read requests."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]],
                 recv: Callable[[], Awaitable[bytes]], qq: int = DEFAULT_SOURCE):
        self._send = send
        self._recv = recv
        self.qq = qq

    async def read_raw(self, zz: int, pbsb: str, msgid: str, master_data: bytes = b"",
                       timeout: float = 3.0, tries: int = 12) -> bytes:
        """Perform one active read, returning the raw slave data bytes. A lost
        arbitration on the shared bus is retried at the next SYN, as ebusd does."""
        drv = await _run_with_retry(
            self._send, self._recv,
            lambda: ReadDriver(zz, pbsb, msgid, self.qq, master_data), timeout, tries)
        assert drv.result is not None
        return drv.result

    async def read(self, msg: MessageDef, timeout: float = 3.0) -> dict:
        """Active-read one message definition and return its labelled values."""
        if not (msg.zz and len(msg.zz) == 2):
            raise ValueError(f"{msg.name}: no destination address to read from")
        slave = await self.read_raw(int(msg.zz, 16), msg.pbsb, msg.msgid, timeout=timeout)
        return msg.decode_labeled(master=b"", slave=slave)

    async def scan(self, zz: int, timeout: float = 3.0) -> ScanResult:
        """Identify a device by reading 0704 (manufacturer, id, SW, HW)."""
        slave = await self.read_raw(zz, SCAN_PBSB, "", timeout=timeout)
        return decode_scan(slave)


class ActiveWriter:
    """Async active writer over an enh link. Writing changes device settings, so
    it is gated: construction requires an explicit `allow_write=True`, and the
    caller is expected to confirm each change. A lost arbitration is retried
    safely (an incomplete frame is rejected on CRC), but a NAK is never retried.
    """

    def __init__(self, send: Callable[[bytes], Awaitable[None]],
                 recv: Callable[[], Awaitable[bytes]], qq: int = DEFAULT_SOURCE,
                 *, allow_write: bool = False):
        if not allow_write:
            raise PermissionError(
                "ActiveWriter is disabled; pass allow_write=True to send writes to the bus")
        self._send = send
        self._recv = recv
        self.qq = qq

    async def write_raw(self, zz: int, pbsb: str, master_data: bytes,
                        timeout: float = 3.0, tries: int = 12) -> None:
        """Write a prepared master payload (id + encoded fields) to a device."""
        await _run_with_retry(
            self._send, self._recv,
            lambda: WriteDriver(zz, pbsb, master_data, self.qq), timeout, tries)

    async def write(self, msg: MessageDef, values: dict, timeout: float = 3.0) -> None:
        """Encode `values` for a write message and send it (needs a 2-hex zz)."""
        if not msg.is_write:
            raise ValueError(f"{msg.name}: not a write message")
        if not (msg.zz and len(msg.zz) == 2):
            raise ValueError(f"{msg.name}: no destination address to write to")
        await self.write_raw(int(msg.zz, 16), msg.pbsb, msg.encode(values), timeout=timeout)
