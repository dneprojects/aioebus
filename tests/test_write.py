"""Write side: master-frame build and the write FSM, driven offline by a
simulated adapter that plays ebusd's active-set sequence: STARTED, echo each
command byte, then a master ACK (0x00). No bus. The engine's write policy (sent
once, uncertain after sending) is in test_engine.py.
"""
import pytest

from aioebus.active import (
    ACK,
    NAK,
    SEND,
    START,
    WriteDriver,
    build_write_frame,
)
from aioebus.frame import BROADCAST, SYN, crc8, escape
from aioebus.transport import RECEIVED, STARTED, EnhDecoder, encode_symbol

QQ = 0x31


def _answer(hexdata: str = "", crc: int | None = None) -> bytes:
    """A slave answer as on the wire (escaped): SN, data, CRC (or a forced CRC)."""
    data = bytes.fromhex(hexdata)
    frame = bytes((len(data),)) + data
    return escape(frame + bytes((crc8(escape(frame)) if crc is None else crc,)))


class _BusSim:
    """Plays the bus around one telegram, following the eBUS shapes: STARTED,
    an echo for every byte we send, then the destination's replies to the
    command (`cmd_replies`, e.g. NAK then ACK) and its answers (`answers`, one
    per request: the first after the ACK, the next after our NAK).

    Every command byte we send is checked against the expected master part:
    after arbitration it continues behind QQ, a repeat after NAK starts at QQ.
    Failure modes: `last_echo` garbles the echo of the final command byte (a
    collision on the CRC), `nak_echo` garbles the echo of our NAK, `idle_after_ack`
    lets the bus go idle (SYN) instead of answering. `sent` records every byte
    we sent, `commands` how often the complete command went out (a resend after
    a new arbitration counts too), `starts` the arbitrations."""

    def __init__(self, frame: bytes, cmd_replies=(ACK,), answers=(), *,
                 last_echo=None, nak_echo=None, idle_after_ack=False):
        self.frame = frame
        self.expect = frame[len(escape(frame[:1])):]  # arbitration already put QQ out
        self.cmd_replies, self.answers = list(cmd_replies), list(answers)
        self.last_echo, self.nak_echo, self.idle_after_ack = last_echo, nak_echo, idle_after_ack
        self.i = 0
        self.sent: list[int] = []
        self.commands = 0  # complete commands sent, across all arbitrations
        self.starts = 0
        self.answered = 0

    def _answer(self) -> bytes:
        self.answered += 1
        return b"".join(encode_symbol(RECEIVED, b) for b in self.answers.pop(0))

    def respond(self, code: int, data: int) -> bytes:
        if code == START:  # a (new) arbitration: the command starts over behind QQ
            self.starts += 1
            self.expect, self.i = self.frame[len(escape(self.frame[:1])):], 0
            return encode_symbol(STARTED, data)
        if code != SEND:
            return b""
        self.sent.append(data)
        if data == SYN:  # never part of an (escaped) command: the bus is released
            return encode_symbol(RECEIVED, SYN)
        if self.i < len(self.expect):  # part of the command
            assert data == self.expect[self.i], (
                f"command byte {self.i}: sent {data:#04x}, expected {self.expect[self.i]:#04x}")
            self.i += 1
            if self.i < len(self.expect):
                return encode_symbol(RECEIVED, data)
            self.commands += 1
            if self.last_echo is not None:
                return encode_symbol(RECEIVED, self.last_echo)
            out = bytearray(encode_symbol(RECEIVED, data))
            if self.cmd_replies:
                reply = self.cmd_replies.pop(0)
                out += encode_symbol(RECEIVED, reply)
                if reply == NAK:  # the master repeats the whole master part
                    self.expect, self.i = self.frame, 0
                elif self.idle_after_ack:
                    out += encode_symbol(RECEIVED, SYN)
                elif self.answers:
                    out += self._answer()
            return bytes(out)
        if data == NAK:  # we NAKed the answer: echo, then the slave repeats
            if self.nak_echo is not None:
                return encode_symbol(RECEIVED, self.nak_echo)
            out = bytearray(encode_symbol(RECEIVED, NAK))
            if self.answers:
                out += self._answer()
            return bytes(out)
        return encode_symbol(RECEIVED, data)


def _run(drv, adapter):
    dec = EnhDecoder()
    pending = list(drv.start())
    guard = 0
    while not drv.done and guard < 2000:
        guard += 1
        reply = bytearray()
        for sym in pending:
            for code, data in dec.feed(sym):
                reply += adapter.respond(code, data)
        pending = drv.feed(bytes(reply)) if reply else []
        if not reply and not pending:
            break
    for sym in pending:  # the closing symbols (ACK/NAK, SYN) still reach the bus
        for code, data in dec.feed(sym):
            adapter.respond(code, data)


MASTER = bytes.fromhex("0e18003c")  # FlowTempDesired = 30.0


def _cmd(zz: int) -> bytes:
    """The whole master part on the wire, QQ ZZ PB SB NN DD CRC."""
    return build_write_frame(zz, "b509", MASTER, QQ)


def test_write_to_slave_waits_for_its_answer():
    # QQ ZZ .. CRC | ACK | SN DD CRC | ACK SYN: the bus is released only after
    # the slave's answer was received and acknowledged
    sim = _BusSim(_cmd(0x38), answers=[_answer("")])
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error is None and drv.result is True
    assert sim.answered == 1
    assert sim.sent[-2:] == [ACK, SYN] and SYN not in sim.sent[:-1]


def test_write_answer_with_data_is_kept():
    sim = _BusSim(_cmd(0x15), answers=[_answer("01")])
    drv = WriteDriver(0x15, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.result is True and drv.answer == b"\x01"


def test_write_to_master_ends_after_ack():
    sim = _BusSim(_cmd(0x10))  # 0x10 is a master address: no answer follows
    drv = WriteDriver(0x10, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error is None and drv.result is True
    assert sim.sent[len(sim.expect):] == [SYN]  # no answer awaited, no ACK sent


def test_broadcast_write_expects_no_ack():
    sim = _BusSim(_cmd(BROADCAST), cmd_replies=())  # nobody acknowledges a broadcast
    drv = WriteDriver(BROADCAST, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error is None and drv.result is True
    assert sim.sent[-1] == SYN


def test_nakd_command_is_repeated_once():
    sim = _BusSim(_cmd(0x38), cmd_replies=(NAK, ACK), answers=[_answer("")])
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error is None and drv.result is True
    # the repeat is the whole master part from QQ (checked byte by byte)
    assert sim.commands == 2 and sim.sent[-len(_cmd(0x38)) - 2:-2] == list(_cmd(0x38))
    assert sim.sent[-2:] == [ACK, SYN]


def test_write_nak_twice_is_error_and_releases_the_bus():
    sim = _BusSim(_cmd(0x38), cmd_replies=(NAK, NAK))
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.done and drv.error == "command NAKed by slave"
    assert sim.commands == 2 and sim.sent[-1] == SYN


def test_more_than_16_data_bytes_is_refused():
    with pytest.raises(ValueError):
        build_write_frame(0x15, "b524", bytes(17), QQ)
    assert build_write_frame(0x15, "b524", bytes(16), QQ)


def test_bad_answer_crc_is_nakd_and_repeated():
    sim = _BusSim(_cmd(0x38), answers=[_answer("", crc=0x12), _answer("")])
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error is None and drv.result is True
    assert sim.answered == 2 and sim.sent[-3:] == [NAK, ACK, SYN]


def test_bad_answer_crc_twice_is_error():
    sim = _BusSim(_cmd(0x38), answers=[_answer("", crc=0x12), _answer("", crc=0x12)])
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    _run(drv, sim)
    assert drv.error == "slave CRC mismatch"


def test_only_a_complete_command_may_have_landed():
    # a command cut off before its CRC cannot be applied (a retry is safe); once
    # the last byte went out it may have been, whatever the echo says
    drv = WriteDriver(0x38, "b509", MASTER, QQ)
    drv.start()
    out = drv.feed(encode_symbol(STARTED, QQ))
    cmd = _cmd(0x38)[1:]
    for b in cmd[:-1]:
        assert not drv.may_have_landed
        out = drv.feed(encode_symbol(RECEIVED, b))
    assert out and drv.may_have_landed  # the CRC byte is being sent now
    drv.feed(encode_symbol(RECEIVED, 0x12))  # garbled echo of the CRC
    assert drv.error and "collision" in drv.error and not drv.accepted
