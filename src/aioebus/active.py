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

from .frame import BROADCAST, ESC, SYN, crc8, escape
from .messages import MessageDef
from .scan import SCAN_PBSB, Identification, ScanResult, decode_identification, decode_scan
from .transport import FAILED, RECEIVED, STARTED, EnhDecoder

DEFAULT_SOURCE = 0x31  # ebusd's own master address on the reference bus (free when ebusd is down)
SEND = 0x1
START = 0x2
ACK = 0x00
NAK = 0xFF
NO_ANSWER = "no answer from slave"
NO_ANSWER_TRIES = 3  # attempts for a read the slave ACKs but leaves unanswered


class ReadError(Exception):
    """An active read failed (arbitration lost, collision, NAK, bad CRC)."""


class WriteUncertain(ReadError):
    """A write failed after its whole command was on the bus: the destination
    may have applied it. Read the value back instead of blindly retrying."""


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
    if len(data) > 16:  # eBUS allows at most 16 master data bytes
        raise ValueError(f"{pbsb}: {len(data)} data bytes, eBUS allows 16")
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


_MASTER_NIBBLES = frozenset((0x0, 0x1, 0x3, 0x7, 0xF))


def is_master(addr: int) -> bool:
    """eBUS master addresses have both nibbles from {0, 1, 3, 7, F}."""
    return (addr >> 4) in _MASTER_NIBBLES and (addr & 0x0F) in _MASTER_NIBBLES


class _CommandDriver:
    """Pure byte-level FSM shared by active reads and writes, by destination:

        slave:     QQ ZZ PB SB NN DD CRC | ACK | SN DD CRC | ACK SYN
        master:    QQ ZZ PB SB NN DD CRC | ACK | SYN
        broadcast: QQ FE PB SB NN DD CRC | SYN

    Arbitrate (START with QQ), send the command (each byte echoed back and
    verified for collision), then follow the destination's shape. A NAKed
    command is repeated once, a slave answer failing its CRC is NAKed once so
    the slave repeats it -- as the eBUS spec (and ebusd) do. No I/O here, so it
    is fully testable against a simulated adapter; `done`/`result`/`error`
    report the outcome.
    """

    def __init__(self, zz: int, pbsb: str, data: bytes, qq: int = DEFAULT_SOURCE):
        # the whole master part, QQ ZZ PB SB NN data CRC; arbitration puts QQ on
        # the bus, so the first send starts after it, a repeat after NAK at QQ
        self._cmd = build_read_frame(zz, pbsb, "", qq, data)
        self._first = len(escape(bytes((qq,))))
        self.qq = qq
        self.zz = zz
        self._dec = EnhDecoder()
        self._state = "arb"
        self._sendpos = self._first
        # the destination ACKed the command (or it was a complete broadcast): a
        # write has been applied, whatever happens to the rest of the telegram
        self.accepted = False
        # the whole command went out at least once: a write may have been applied
        # even without a visible ACK, so it must never be sent again
        self.may_have_landed = False
        self._cmd_repeated = False
        self._answer_nakd = False
        self._slave = bytearray()  # unescaped slave answer so far
        self._esc = False  # an escape prefix arrived, its second byte is pending
        self.done = False
        self.result = None
        self.error: str | None = None
        self.lost_to: int | None = None  # winning master address when arbitration is lost

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
                self.lost_to = data  # the master that won the bus (for passive capture)
                return self._fail(f"arbitration lost to {data:#04x}")
            return []  # ignore SYN/other while waiting to win

        if self._state == "cmd":  # verify echo of the byte we just sent
            if code != RECEIVED or data != self._cmd[self._sendpos - 1]:
                return self._fail("collision: echo mismatch while sending command")
            return self._send_next_cmd()

        if self._state == "ack":  # the destination's ACK/NAK of our command
            if code == RECEIVED and data == ACK:
                self.accepted = True
                if is_master(self.zz):  # master/master: no answer follows
                    return self._complete(b"")
                self._state = "res"
                return []
            if code == RECEIVED and data == NAK and not self._cmd_repeated:
                # rejected (e.g. it saw a bad CRC), so nothing was applied: send the
                # whole master part again from QQ, without a new arbitration
                self._cmd_repeated = True
                self._sendpos = 0
                self._state = "cmd"
                return self._send_next_cmd()
            if code == RECEIVED and data == NAK:
                self._fail("command NAKed by slave")
                return [_symbol2(SEND, SYN)]  # the bus is ours: release it
            return self._fail("unexpected symbol waiting for command ACK")

        if self._state == "nak_echo":  # our own NAK coming back; the repeat follows
            if code == RECEIVED and data == NAK:
                self._state = "res"
                return []
            return self._fail("collision: echo mismatch while sending NAK")

        if self._state == "res" and code == RECEIVED:
            return self._answer_byte(data)
        return []

    def _send_next_cmd(self) -> list[bytes]:
        if self._sendpos >= len(self._cmd):
            if self.zz == BROADCAST:  # nobody acknowledges a broadcast
                self.accepted = True
                return self._complete(b"")
            self._state = "ack"
            return []
        b = self._cmd[self._sendpos]
        self._sendpos += 1
        if self._sendpos == len(self._cmd):
            self.may_have_landed = True
        return [_symbol2(SEND, b)]

    def _answer_byte(self, data: int) -> list[bytes]:
        # a raw SYN never occurs inside a frame: the slave did not answer and the
        # bus went idle, so give up now instead of waiting for a 0xAA-byte answer
        if data == SYN:
            return self._fail(NO_ANSWER)
        # unescape byte by byte, so a frame ending in an escaped byte (e.g. a CRC
        # of 0xAA sent as A9 01) is not judged complete on its first half
        if self._esc:
            self._esc = False
            self._slave.append(SYN if data == 0x01 else ESC if data == 0x00 else data)
        elif data == ESC:
            self._esc = True
            return []
        else:
            self._slave.append(data)
        u = self._slave
        if len(u) < u[0] + 2:  # SN + SN data + CRC
            return []
        sn = u[0]
        sdata = bytes(u[1:1 + sn])
        if crc8(escape(bytes((sn,)) + sdata)) == u[1 + sn]:
            # ACK the good answer, then release the bus with a SYN
            return self._complete(sdata, [_symbol2(SEND, ACK)])
        if not self._answer_nakd:  # NAK it once; the slave repeats its answer
            self._answer_nakd = True
            self._slave.clear()
            self._state = "nak_echo"
            return [_symbol2(SEND, NAK)]
        self.error = "slave CRC mismatch"
        self.done = True
        return [_symbol2(SEND, NAK), _symbol2(SEND, SYN)]

    def _complete(self, answer: bytes, before_syn: list[bytes] | None = None) -> list[bytes]:
        self.result = self._result(answer)
        self.done = True
        return [*(before_syn or []), _symbol2(SEND, SYN)]

    def _result(self, answer: bytes):  # subclass: what the caller gets back
        return answer


class ReadDriver(_CommandDriver):
    """Active read: the result is the slave's verified answer data."""

    def __init__(self, zz: int, pbsb: str, msgid: str, qq: int = DEFAULT_SOURCE,
                 master_data: bytes = b""):
        super().__init__(zz, pbsb, bytes.fromhex(msgid) + master_data, qq)


class WriteDriver(_CommandDriver):
    """Active write: done once the destination accepted it -- for a slave that
    includes receiving (and acknowledging) its answer before the bus is
    released; the result is True."""

    def __init__(self, zz: int, pbsb: str, master_data: bytes, qq: int = DEFAULT_SOURCE):
        super().__init__(zz, pbsb, master_data, qq)  # master_data already includes the id
        self.answer: bytes | None = None  # the slave's answer data, if any

    def _result(self, answer: bytes):
        self.answer = answer
        return True


async def _run_with_retry(send, recv, make_driver, timeout: float, tries: int):
    """Run a command driver to completion, retrying only when the shared bus was
    not actually acquired (lost arbitration / collision before we owned it).
    Returns the finished driver; a NAK or other error is returned, not retried,
    and is never retried for a write because the slave may already have applied
    one copy. The bus acquire itself is safe to retry: an incomplete master frame
    is rejected on CRC, so no half-write lands. A read the slave ACKed but did not
    answer is retried too (up to NO_ANSWER_TRIES attempts, like ebusd's resend):
    some devices skip the odd answer, and the read has no side effect."""
    last = "no attempt"
    no_answer = 0
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
            drv.error = last = "timeout"
        if drv.error is None:
            return drv
        last = drv.error
        if isinstance(drv, WriteDriver):
            if drv.accepted:
                return drv  # the destination ACKed it: applied, only the tail failed
            if drv.may_have_landed:  # it may have landed: never resend
                raise WriteUncertain(last)
        if "arbitration lost" in last or "collision" in last:
            await asyncio.sleep(0.05)  # let the bus reach a fresh SYN
            continue
        if last == "timeout":
            continue
        if last == NO_ANSWER and isinstance(drv, ReadDriver):
            no_answer += 1
            if no_answer < NO_ANSWER_TRIES:
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

    async def read_identification(
        self, zz: int, timeout: float = 2.0
    ) -> Identification | None:
        """Read the Vaillant extended identification (article/serial) from b509
        24/25/26. Returns None if the device does not answer these reads."""
        try:
            f24 = await self.read_raw(zz, "b509", "24", timeout=timeout, tries=3)
            f25 = await self.read_raw(zz, "b509", "25", timeout=timeout, tries=3)
            f26 = await self.read_raw(zz, "b509", "26", timeout=timeout, tries=3)
            f27 = await self.read_raw(zz, "b509", "27", timeout=timeout, tries=3)
        except ConnectionError:
            raise  # the link itself is gone: the caller must reconnect, not skip
        except (ReadError, TimeoutError, OSError):
            return None
        return decode_identification(f24, f25, f26, f27)


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
