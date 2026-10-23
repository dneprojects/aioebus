"""Write side (Phase 4c): value encoding, master-frame build, write FSM, safety.

The write FSM is driven offline by a simulated adapter that plays ebusd's
active-set sequence: STARTED, echo each command byte, then a master ACK (0x00).
No bus. ActiveWriter is gated behind an explicit allow_write.
"""
import pytest

from aioebus.active import (
    ACK,
    SEND,
    START,
    ActiveWriter,
    WriteDriver,
    build_write_frame,
)
from aioebus.codecs import decode, encode
from aioebus.frame import escape
from aioebus.messages import parse_csv
from aioebus.transport import RECEIVED, STARTED, EnhDecoder, encode_symbol

QQ = 0x31
HDR = "type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment"


def test_encode_numeric_roundtrips():
    assert encode("UCH", 15) == bytes.fromhex("0f")
    assert encode("D1C", 30.0) == bytes.fromhex("3c")
    assert encode("D2C", 27.8125) == bytes.fromhex("bd01")
    assert encode("SIN", 50.0, divisor=10) == bytes.fromhex("f401")
    for spec, raw in [("UCH", "0f"), ("D1C", "3c"), ("D2C", "bd01")]:
        assert encode(spec, decode(spec, raw)) == bytes.fromhex(raw)


def test_encode_rejects_non_numeric():
    with pytest.raises(NotImplementedError):
        encode("STR:3", "abc")


def test_message_encode_builds_master_data():
    msg = parse_csv(HDR + "\n"
                    "w,,,FlowTempDesired,,,38,b509,0e1800,value,,D1C,,°C,\n")[0]
    assert msg.encode({"FlowTempDesired": 30.0}).hex() == "0e18003c"  # id + D1C(30)


def test_message_encode_enum_label():
    msg = parse_csv(HDR + "\n"
                    "w,,,Mode,,,38,b510,00,value,,UCH,0=auto;1=off;2=heat,,\n")[0]
    assert msg.encode({"Mode": "heat"}).hex() == "0002"       # id 00 + UCH 2
    assert msg.encode({"Mode": 1}).hex() == "0001"             # raw int also ok


class _WriteAdapter:
    def __init__(self, cmd_bytes: bytes):
        self.cmd = cmd_bytes
        self.i = 0

    def respond(self, code: int, data: int) -> bytes:
        out = bytearray()
        if code == START:
            out += encode_symbol(STARTED, data)
        elif code == SEND:
            if self.i < len(self.cmd):
                out += encode_symbol(RECEIVED, self.cmd[self.i])  # echo
                self.i += 1
                if self.i == len(self.cmd):
                    out += encode_symbol(RECEIVED, ACK)           # slave ACKs the write
        return bytes(out)


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


def test_write_fsm_roundtrip():
    master = bytes.fromhex("0e18003c")  # FlowTempDesired = 30.0
    cmd = build_write_frame(0x38, "b509", master, QQ)[len(escape(bytes((QQ,)))):]
    drv = WriteDriver(0x38, "b509", master, QQ)
    _run(drv, _WriteAdapter(cmd))
    assert drv.error is None and drv.result is True


def test_write_nak_is_error():
    drv = WriteDriver(0x38, "b509", bytes.fromhex("0e18003c"), QQ)
    drv.start()
    drv.feed(encode_symbol(STARTED, QQ))
    cmd = drv._cmd
    # echo all command bytes, then NAK instead of ACK
    for b in cmd:
        drv.feed(encode_symbol(RECEIVED, b))
    drv.feed(encode_symbol(RECEIVED, 0xFF))
    assert drv.done and drv.error and "NAK" in drv.error


def test_active_writer_requires_optin():
    async def _noop(_):
        return None

    async def _recv():
        return b""

    with pytest.raises(PermissionError):
        ActiveWriter(_noop, _recv)
    ActiveWriter(_noop, _recv, allow_write=True)  # explicit opt-in is fine
