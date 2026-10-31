"""Active-read FSM (Phase 4b) driven offline by a simulated enh adapter.

The adapter double plays ebusd's confirmed protocol_direct sequence: reply to
START with STARTED, echo every SEND byte back as RECEIVED, then deliver the
master ACK and the slave frame. The ReadDriver must come out with the right
slave data and decode it. No socket, no bus.
"""
from aioebus.active import ACK, SEND, START, ReadDriver, build_read_frame
from aioebus.frame import crc8, escape
from aioebus.messages import parse_csv
from aioebus.transport import RECEIVED, STARTED, EnhDecoder, encode_symbol

QQ = 0x31
ZZ, PBSB, MSGID = 0x38, "b509", "0d1800"  # eloBlock FlowTemp read


def _slave_wire(slave_data_hex: str) -> bytes:
    """Build the RECEIVED byte values for: master ACK + SN + data + slave CRC."""
    sdata = bytes.fromhex(slave_data_hex)
    sn = len(sdata)
    scrc = crc8(escape(bytes((sn,)) + sdata))
    return bytes((ACK, sn)) + sdata + bytes((scrc,))


class FakeAdapter:
    def __init__(self, cmd_bytes: bytes, slave_wire: bytes):
        self.cmd = cmd_bytes
        self.slave = slave_wire
        self.i = 0

    def respond(self, code: int, data: int) -> bytes:
        out = bytearray()
        if code == START:
            out += encode_symbol(STARTED, data)         # arbitration won
        elif code == SEND:
            if self.i < len(self.cmd):
                out += encode_symbol(RECEIVED, self.cmd[self.i])  # echo the sent byte
                self.i += 1
                if self.i == len(self.cmd):              # last cmd byte -> answer
                    for b in self.slave:
                        out += encode_symbol(RECEIVED, b)
            # SEND after the answer (our ACK/SYN) needs no reply
        return bytes(out)


def _run(drv: ReadDriver, adapter: FakeAdapter) -> None:
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


def test_active_read_roundtrip():
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    adapter = FakeAdapter(cmd, _slave_wire("910100"))
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, adapter)
    assert drv.error is None
    assert drv.result == bytes.fromhex("910100")


def test_active_read_decodes_value():
    csv = ("type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment\n"
           "r1,elo,,FlowTemp,,,38,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,\n")
    msg = parse_csv(csv)[0]
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    adapter = FakeAdapter(cmd, _slave_wire("910100"))
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, adapter)
    assert msg.decode_labeled(slave=drv.result) == {"FlowTemp": 25.0625}


def test_arbitration_lost():
    from aioebus.transport import FAILED
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    drv.start()
    out = drv.feed(encode_symbol(FAILED, 0x10))  # another master won
    assert drv.done and drv.error and "arbitration lost" in drv.error
    assert out == []


def test_slave_crc_aa_is_unescaped_before_completion():
    # a slave CRC of 0xAA travels as A9 01; the frame must not be judged complete
    # (and fail the CRC) on the escape prefix alone
    data = next(bytes((d,)) for d in range(256)
                if d not in (0xA9, 0xAA) and crc8(escape(bytes((1, d)))) == 0xAA)
    wire = bytes((ACK,)) + escape(bytes((1,)) + data + bytes((0xAA,)))
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, FakeAdapter(cmd, wire))
    assert drv.error is None
    assert drv.result == data


def test_slave_data_with_escaped_bytes():
    # 0xA9 and 0xAA inside the answer travel escaped (A9 00 / A9 01)
    data = bytes((0xA9, 0x01, 0xAA, 0x00))
    sn = bytes((len(data),))
    crc = crc8(escape(sn + data))
    wire = bytes((ACK,)) + escape(sn + data + bytes((crc,)))
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, FakeAdapter(cmd, wire))
    assert drv.error is None
    assert drv.result == data


def test_read_command_nak_fails():
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, FakeAdapter(cmd, bytes((0xFF,))))
    assert drv.done and drv.error == "command NAKed by slave"


def test_unanswered_read_fails_on_syn():
    # the slave ACKs the command but never answers: the bus goes idle (SYN), so
    # the read fails at once instead of waiting for a 0xAA-byte answer
    cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    _run(drv, FakeAdapter(cmd, bytes((ACK, 0xAA))))
    assert drv.done and drv.error == "no answer from slave"


class _FlakyLink:
    """Async send/recv pair whose slave leaves the first `misses` reads unanswered
    (ACK, then the bus goes idle) and answers the next one."""

    def __init__(self, misses: int, answer_hex: str):
        import asyncio
        self.q: asyncio.Queue = asyncio.Queue()
        self.misses, self.answer, self.attempts = misses, answer_hex, 0
        self._dec = EnhDecoder()
        self._adapter: FakeAdapter | None = None

    async def send(self, data: bytes) -> None:
        for code, value in self._dec.feed(data):
            if code == START:
                self.attempts += 1
                cmd = build_read_frame(ZZ, PBSB, MSGID, QQ)[len(escape(bytes((QQ,)))):]
                slave = (bytes((ACK, 0xAA)) if self.attempts <= self.misses
                         else _slave_wire(self.answer))
                self._adapter = FakeAdapter(cmd, slave)
            reply = self._adapter.respond(code, value) if self._adapter else b""
            if reply:
                self.q.put_nowait(reply)

    async def recv(self) -> bytes:
        return await self.q.get()


def test_unanswered_read_is_retried():
    # a device skipping the odd answer must not lose e.g. its identification
    import asyncio

    from aioebus.active import NO_ANSWER_TRIES, ActiveReader, ReadError

    link = _FlakyLink(misses=NO_ANSWER_TRIES - 1, answer_hex="910100")
    result = asyncio.run(ActiveReader(link.send, link.recv).read_raw(ZZ, PBSB, MSGID))
    assert result == bytes.fromhex("910100") and link.attempts == NO_ANSWER_TRIES

    link = _FlakyLink(misses=99, answer_hex="910100")  # never answers: bounded
    try:
        asyncio.run(ActiveReader(link.send, link.recv).read_raw(ZZ, PBSB, MSGID))
        raise AssertionError("expected ReadError")
    except ReadError as err:
        assert "no answer" in str(err)
    assert link.attempts == NO_ANSWER_TRIES


def test_identification_reraises_a_lost_link():
    # a dropped link is not "device has no identification": the caller must see it
    import asyncio

    import pytest

    from aioebus.active import ActiveReader

    async def recv():
        raise ConnectionResetError("adapter closed the connection")

    async def send(_data):
        return None

    with pytest.raises(ConnectionResetError):
        asyncio.run(ActiveReader(send, recv).read_identification(0x08))


def test_collision_on_echo_mismatch():
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    drv.start()
    drv.feed(encode_symbol(STARTED, QQ))          # won; now sending first cmd byte
    out = drv.feed(encode_symbol(RECEIVED, 0x99))  # wrong echo
    assert drv.done and "collision" in drv.error
    assert out == []
