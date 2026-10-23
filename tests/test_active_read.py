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


def test_collision_on_echo_mismatch():
    drv = ReadDriver(ZZ, PBSB, MSGID, QQ)
    drv.start()
    drv.feed(encode_symbol(STARTED, QQ))          # won; now sending first cmd byte
    out = drv.feed(encode_symbol(RECEIVED, 0x99))  # wrong echo
    assert drv.done and "collision" in drv.error
    assert out == []
