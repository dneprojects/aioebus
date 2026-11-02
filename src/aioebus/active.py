"""Active transactions over the enhanced adapter: the master request and the
byte-level state machine that drives one read or write. The FSM is ported from
ebusd's protocol_direct.cpp (active-get path):

    arbitrate(QQ via START) -> STARTED/FAILED
    send ZZ PB SB NN DD   (each SEND byte is echoed back as RECEIVED, verified)
    send CRC              (echoed, verified)
    recv master ACK (0x00) | NAK (0xff)
    recv slave: SN, SN data bytes, SCRC  (unescaped); verify CRC
    send ACK (0x00)       (echoed)
    send SYN              (releases the bus)

`qq` defaults to 0x31 -- the address ebusd itself uses on the reference bus, so
it is a proven-good master address that is free exactly when ebusd is stopped
(the single-client adapter needs ebusd down anyway). The drivers are pure (no
I/O, testable against a simulated adapter); the engine pumps bytes between them
and the adapter and owns the retry policy.
"""
from __future__ import annotations

from .frame import BROADCAST, ESC, SYN, crc8, escape, is_master
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


# START with SYN as the address withdraws a pending arbitration (enh protocol), so
# an abandoned START cannot win the bus later with nobody to continue the telegram
CANCEL_ARBITRATION = _symbol2(START, SYN)
RELEASE_BUS = _symbol2(SEND, SYN)  # a SYN from the bus owner ends its turn


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


def build_write_frame(
    zz: int, pbsb: str, master_data: bytes, qq: int = DEFAULT_SOURCE
) -> bytes:
    """Build the on-wire master frame for a write; `master_data` already includes
    the message id and the encoded field bytes (see MessageDef.encode)."""
    return build_read_frame(zz, pbsb, "", qq, master_data)


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
        self.result: bytes | bool | None = None
        self.error: str | None = None
        # the bus was contended (arbitration lost, collision): worth starting
        # over -- for a write only while it cannot have landed (the engine checks)
        self.retryable = False
        self.lost_to: int | None = None  # winning master address when arbitration is lost

    def start(self) -> list[bytes]:
        """Initial symbols to send: the arbitration START with QQ."""
        return [_symbol2(START, self.qq)]

    @property
    def arbitrating(self) -> bool:
        """START sent, the bus not yet won or lost: the adapter still holds it."""
        return self._state == "arb" and not self.done

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

    def _fail(self, msg: str, *, retryable: bool = False) -> list[bytes]:
        self.error = msg
        self.retryable = retryable
        self.done = True
        return []

    def abort(self, reason: str) -> None:
        """Give the transaction up from outside (e.g. past its deadline)."""
        if not self.done:
            self._fail(reason)

    def _step(self, code: int, data: int) -> list[bytes]:
        if self._state == "arb":
            if code == STARTED:
                self._state = "cmd"
                return self._send_next_cmd()
            if code == FAILED:
                self.lost_to = data  # the master that won the bus (for passive capture)
                return self._fail(f"arbitration lost to {data:#04x}", retryable=True)
            return []  # ignore SYN/other while waiting to win

        if self._state == "cmd":  # verify echo of the byte we just sent
            if code != RECEIVED or data != self._cmd[self._sendpos - 1]:
                return self._fail("collision: echo mismatch while sending command",
                                  retryable=True)
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
        self.request = bytes.fromhex(msgid) + master_data  # id + arguments
        super().__init__(zz, pbsb, self.request, qq)


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
