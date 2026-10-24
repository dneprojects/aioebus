"""Unified engine: passive decode caches values, and explicit active reads are
interleaved at bus idle. Driven by a fake async link (no pytest-asyncio)."""
import asyncio
import contextlib

from aioebus.active import ACK, SEND, START, build_read_frame
from aioebus.engine import EbusEngine, _Active, value_key
from aioebus.frame import crc8, escape
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry
from aioebus.poll import PollScheduler
from aioebus.transport import (
    FAILED,
    RECEIVED,
    STARTED,
    SYN,
    EnhDecoder,
    encode_received_frame,
    encode_symbol,
)

CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38
r1,,,FlowTemp,,,,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,
r1,,,Statenumber,,,,b509,0dab00,value,,UCH,0=kein_bedarf;31=sommer,,
u,bc,,Outsidetemp,,,fe,b516,01,value,,D2B,,°C,
"""
REG = MessageRegistry(parse_csv(CSV))
BY = {m.name: m for m in parse_csv(CSV)}
QQ = 0x31


def _slave_wire(hexdata: str) -> bytes:
    sdata = bytes.fromhex(hexdata)
    sn = len(sdata)
    scrc = crc8(escape(bytes((sn,)) + sdata))
    return bytes((ACK, sn)) + sdata + bytes((scrc,))


class _Adapter:
    """Plays the adapter side of ONE active read: STARTED, echo, ACK + slave."""

    def __init__(self, cmd: bytes, slave: bytes):
        self.cmd, self.slave, self.i = cmd, slave, 0
        self._dec = EnhDecoder()

    def on_send(self, sent: bytes) -> bytes:
        out = bytearray()
        for code, data in self._dec.feed(sent):
            if code == START:
                out += encode_symbol(STARTED, data)
            elif code == SEND and self.i < len(self.cmd):
                out += encode_symbol(RECEIVED, self.cmd[self.i])
                self.i += 1
                if self.i == len(self.cmd):
                    for b in self.slave:
                        out += encode_symbol(RECEIVED, b)
        return bytes(out)


class _Link:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.adapter: _Adapter | None = None

    async def send(self, data: bytes) -> None:
        if self.adapter is not None:
            resp = self.adapter.on_send(data)
            if resp:
                self.q.put_nowait(resp)

    async def recv(self) -> bytes:
        return await self.q.get()

    def inject(self, data: bytes) -> None:
        self.q.put_nowait(data)


def test_passive_decode_caches():
    async def inner():
        link = _Link()
        eng = EbusEngine(link.send, link.recv, REG)
        task = asyncio.create_task(eng.run())
        link.inject(encode_received_frame(bytes.fromhex("3138b509030d1800740003910100bb00")))
        await asyncio.sleep(0.05)
        assert eng.cache[value_key(BY["FlowTemp"], "FlowTemp")] == 25.0625
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


class _ArbLossAdapter:
    """On our arbitration START, reply FAILED(winner) then the winner's telegram
    (minus its already-arbitrated first byte) and a closing SYN."""

    def __init__(self, winner: int, rest_hex: str):
        self.winner, self.rest, self._dec = winner, bytes.fromhex(rest_hex), EnhDecoder()
        self.fired = False

    def on_send(self, sent: bytes) -> bytes:
        out = bytearray()
        for code, _data in self._dec.feed(sent):
            if code == START and not self.fired:
                self.fired = True
                out += encode_symbol(FAILED, self.winner)
                for b in self.rest:
                    out += encode_symbol(RECEIVED, b)
                out += encode_symbol(RECEIVED, SYN)
        return bytes(out)


def test_stats_track_connection_and_bus_activity():
    """A decoded bus telegram counts as activity and marks the link connected;
    an unanswered poll is counted as a failed poll."""
    async def inner():
        link = _Link()
        eng = EbusEngine(link.send, link.recv, REG)
        assert eng.stats.connected is False
        task = asyncio.create_task(eng.run())
        link.inject(encode_received_frame(bytes.fromhex("3138b509030d1800740003910100bb00")))
        await asyncio.sleep(0.05)
        assert eng.stats.connected is True
        assert eng.stats.telegrams == 1
        assert eng.stats.last_telegram is not None
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)
        assert eng.stats.connected is False

        msg = BY["Statenumber"]
        sched = PollScheduler([msg], base_interval=10.0)
        eng2 = EbusEngine(link.send, link.recv, REG, scheduler=sched)

        class _FailedDriver:
            error = "timeout waiting for slave"

        act = _Active(lambda: _FailedDriver(), msg, None)  # future None -> a poll
        act.driver = _FailedDriver()
        eng2._active = act
        await eng2._finish_active()
        assert eng2.stats.polls_failed == 1

    asyncio.run(inner())


def test_arbitration_loss_captures_winner_passively():
    """When our poll loses arbitration, the telegram that won is decoded
    passively (seeded with the winner's address) instead of being discarded."""
    async def inner():
        link = _Link()
        # real outside-temp broadcast 10 fe b509... -> here b516 01 c0 0d (13.75 C);
        # winner 0x10, rest is the telegram without its leading (arbitrated) 0x10
        link.adapter = _ArbLossAdapter(0x10, "feb5160301c00d12")
        eng = EbusEngine(link.send, link.recv, REG)
        task = asyncio.create_task(eng.run())
        rtask = asyncio.create_task(eng.read(BY["Statenumber"], timeout=0.2))
        await asyncio.sleep(0)
        link.inject(encode_symbol(RECEIVED, SYN))  # idle -> our read starts -> loses arbitration
        await asyncio.sleep(0.05)
        assert eng.cache.get(value_key(BY["Outsidetemp"], "Outsidetemp")) == 13.75
        rtask.cancel()
        eng.stop()
        link.inject(b"")
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(rtask, 0.5)
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_failed_poll_is_recorded_not_restuck():
    """A poll the device does not answer must update its scheduler timer, so the
    scheduler moves on instead of handing back the same dead register forever
    (which would starve every other register and stall passive decode)."""
    async def inner():
        link = _Link()
        msg = BY["Statenumber"]
        sched = PollScheduler([msg], base_interval=10.0)
        eng = EbusEngine(link.send, link.recv, REG, scheduler=sched)
        key = (msg.zz, msg.name)
        assert sched._items[key].last_update == 0.0  # due immediately

        class _FailedDriver:
            error = "timeout waiting for slave"

        act = _Active(lambda: _FailedDriver(), msg, None)  # future None -> a poll
        act.driver = _FailedDriver()
        eng._active = act
        await eng._finish_active()
        assert sched._items[key].last_update > 0.0  # attempt recorded -> not re-due now

    asyncio.run(inner())


def test_active_read_interleaved():
    async def inner():
        link = _Link()
        msg = BY["Statenumber"]
        cmd = build_read_frame(0x38, "b509", "0dab00", QQ)[len(escape(bytes((QQ,)))):]
        link.adapter = _Adapter(cmd, _slave_wire("1f"))  # 0x1f -> "sommer"
        eng = EbusEngine(link.send, link.recv, REG)
        task = asyncio.create_task(eng.run())
        rtask = asyncio.create_task(eng.read(msg, timeout=2))
        await asyncio.sleep(0)                      # let the read enqueue
        link.inject(encode_symbol(RECEIVED, SYN))   # bus idle -> engine starts the read
        result = await asyncio.wait_for(rtask, 2)
        assert result == {"Statenumber": "sommer"}
        assert eng.cache[value_key(msg, "Statenumber")] == "sommer"
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())
