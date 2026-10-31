"""Unified engine: passive decode caches values, and explicit active reads are
interleaved at bus idle. Driven by a fake async link (no pytest-asyncio)."""
import asyncio
import contextlib

import pytest

from aioebus.active import ACK, SEND, START, WriteUncertain, build_read_frame
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
r1,,,FlowTemp,,,,b509,0d1800,value,,D2C,,Â°C,,ign,,IGN:1,,,
r1,,,Statenumber,,,,b509,0dab00,value,,UCH,0=kein_bedarf;31=sommer,,
u,bc,,Outsidetemp,,,fe,b516,01,value,,D2B,,Â°C,
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
        eng.ACTIVE_DEADLINE = 0.1  # below the request timeout, so the read starts
        eng.START_MARGIN = 0.0
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


def test_stuck_transaction_hits_deadline():
    """A transaction the bus never completes (here: no reply to our START) must
    not hold the engine forever: past the deadline it fails like an unanswered
    poll and the engine returns to passive decoding."""
    async def inner():
        link = _Link()  # no adapter: nothing ever answers our START
        msg = BY["Statenumber"]
        sched = PollScheduler([msg], base_interval=10.0)
        eng = EbusEngine(link.send, link.recv, REG, scheduler=sched)
        eng.ACTIVE_DEADLINE = 0.05
        task = asyncio.create_task(eng.run())
        link.inject(encode_symbol(RECEIVED, SYN))  # idle -> the poll starts
        await asyncio.sleep(0.02)
        assert eng._active is not None
        await asyncio.sleep(0.06)
        link.inject(encode_symbol(RECEIVED, SYN))  # next bus event, past the deadline
        await asyncio.sleep(0.02)
        assert eng._active is None
        assert eng.stats.polls_failed == 1
        link.inject(encode_received_frame(bytes.fromhex("3138b509030d1800740003910100bb00")))
        await asyncio.sleep(0.02)
        assert eng.cache[value_key(BY["FlowTemp"], "FlowTemp")] == 25.0625
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_timed_out_request_is_withdrawn():
    """A request the caller gave up on is removed from the queue, so it is never
    sent later (a write must not land after the caller saw it fail)."""
    async def inner():
        link = _Link()
        eng = EbusEngine(link.send, link.recv, REG)  # not running: nothing is dequeued
        with contextlib.suppress(TimeoutError):
            await eng.read(BY["Statenumber"], timeout=0.01)
        assert not eng._queue

    asyncio.run(inner())


class _AlwaysLoseAdapter:
    """Every START loses arbitration; records when each START was sent."""

    def __init__(self):
        self._dec = EnhDecoder()
        self.starts: list[float] = []

    def on_send(self, sent: bytes) -> bytes:
        out = bytearray()
        for code, _data in self._dec.feed(sent):
            if code == START:
                self.starts.append(asyncio.get_running_loop().time())
                out += encode_symbol(FAILED, 0x10)
        return bytes(out)


def test_abandoned_request_is_not_retried():
    """A request that keeps losing arbitration must stop once its caller timed
    out -- a write may not go out after HA already reported it as failed."""
    async def inner():
        link = _Link()
        link.adapter = adapter = _AlwaysLoseAdapter()
        eng = EbusEngine(link.send, link.recv, REG)
        eng.ACTIVE_DEADLINE = 0.01
        eng.START_MARGIN = 0.0
        eng.MAX_ATTEMPTS = 1000  # retries alone would outlast the caller
        task = asyncio.create_task(eng.run())
        loop = asyncio.get_running_loop()
        rtask = asyncio.create_task(eng.read(BY["Statenumber"], timeout=0.1))
        gave_up: list[float] = []
        rtask.add_done_callback(lambda _t: gave_up.append(loop.time()))
        end = loop.time() + 0.3
        while loop.time() < end:  # a busy bus: idle SYNs keep coming
            link.inject(encode_symbol(RECEIVED, SYN))
            await asyncio.sleep(0.005)
        with contextlib.suppress(TimeoutError):
            await rtask
        assert adapter.starts, "the read never started"
        assert not [t for t in adapter.starts if t > gave_up[0]]
        assert not eng._queue
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_request_without_time_to_finish_is_not_started():
    """With less time left than one transaction may take, the request fails at
    once instead of starting and possibly landing after the caller gave up."""
    async def inner():
        link = _Link()
        link.adapter = adapter = _AlwaysLoseAdapter()
        eng = EbusEngine(link.send, link.recv, REG)  # deadline 3 s > timeout 1 s
        task = asyncio.create_task(eng.run())
        rtask = asyncio.create_task(eng.read(BY["Statenumber"], timeout=1.0))
        await asyncio.sleep(0)
        link.inject(encode_symbol(RECEIVED, SYN))
        try:
            await asyncio.wait_for(rtask, 0.5)  # fails long before its own timeout
        except TimeoutError as err:
            assert "no time left" in str(err)
        assert adapter.starts == []
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_empty_answer_counts_as_unanswered_poll():
    """A slave answering a read with no data (SN=0) delivers no value: the poll
    is backed off like an unanswered one, nothing None is cached."""
    async def inner():
        link = _Link()
        msg = BY["Statenumber"]
        cmd = build_read_frame(0x38, "b509", "0dab00", QQ)[len(escape(bytes((QQ,)))):]
        link.adapter = _Adapter(cmd, _slave_wire(""))
        sched = PollScheduler([msg], base_interval=10.0)
        eng = EbusEngine(link.send, link.recv, REG, scheduler=sched)
        task = asyncio.create_task(eng.run())
        link.inject(encode_symbol(RECEIVED, SYN))
        await asyncio.sleep(0.05)
        assert eng.stats.polls_failed == 1 and eng.stats.polls_ok == 0
        assert value_key(msg, "Statenumber") not in eng.cache
        assert sched._items[(msg.zz, msg.name)].fails == 1
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


_Z1 = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,"
                "divisor/values,unit,comment\n"
                "w,,,Z1DayTemp,,,15,b524,02,temp,,EXP,,Â°C,\n")[0]


def _engine_write(sim_kwargs: dict):
    """Write Z1DayTemp=21.5 through the engine against a simulated bus; returns
    (simulator, engine, exception or None)."""
    from test_write import _BusSim

    frame = build_read_frame(0x15, "b524", "", QQ, _Z1.encode({"Z1DayTemp": 21.5}))
    sim = _BusSim(frame, **sim_kwargs)

    async def inner():
        link = _Link()

        class _Wrap:
            _dec = EnhDecoder()

            def on_send(self, data: bytes) -> bytes:
                return b"".join(sim.respond(c, d) for c, d in self._dec.feed(data))

        link.adapter = _Wrap()
        eng = EbusEngine(link.send, link.recv, REG, allow_write=True)
        eng.ACTIVE_DEADLINE = 0.5
        task = asyncio.create_task(eng.run())
        wtask = asyncio.create_task(eng.write(_Z1, {"Z1DayTemp": 21.5}, timeout=1.5))
        await asyncio.sleep(0)
        for _ in range(40):  # idle SYNs keep coming, as on a real bus
            link.inject(encode_symbol(RECEIVED, SYN))
            await asyncio.sleep(0.02)
        err = None
        try:
            await asyncio.wait_for(wtask, 2)
        except Exception as exc:
            err = exc
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)
        return eng, err

    eng, err = asyncio.run(inner())
    return sim, eng, err


def test_engine_write_to_slave_completes_after_answer():
    """The path Home Assistant uses: a write through the engine is reported done
    only after the slave answered; its closing ACK/SYN echoes are harmless."""
    from test_write import _answer

    sim, eng, err = _engine_write({"answers": [_answer("")]})
    assert err is None and eng.stats.writes == 1 and sim.answered == 1
    assert sim.commands == 1 and sim.sent[-2:] == [ACK, SYN]


def test_engine_accepted_write_with_broken_tail_is_sent_once():
    # ACKed = applied; a garbled echo of our NAK afterwards is a "collision" the
    # engine would otherwise retry -- re-sending the setpoint up to 12 times
    from test_write import _answer

    sim, eng, err = _engine_write({"answers": [_answer("", crc=0x12)], "nak_echo": 0x12})
    assert err is None and eng.stats.writes == 1
    assert sim.commands == 1


def test_engine_write_colliding_on_its_last_byte_is_sent_once():
    # the whole command was on the bus: it may have landed -- say so, never resend
    sim, _eng, err = _engine_write({"last_echo": 0x12})
    assert isinstance(err, WriteUncertain)
    assert sim.commands == 1 and sim.starts == 1


class _EchoOnly:
    """Echoes what we send and wins every arbitration, nothing else; the test
    injects the destination's replies itself (to delay or drop them)."""

    def __init__(self):
        self._dec = EnhDecoder()
        self.sent: list[int] = []
        self.starts = 0

    def on_send(self, data: bytes) -> bytes:
        out = bytearray()
        for code, value in self._dec.feed(data):
            if code == START:
                self.starts += 1
                out += encode_symbol(STARTED, value)
            elif code == SEND:
                self.sent.append(value)
                out += encode_symbol(RECEIVED, value)
        return bytes(out)


def test_engine_late_ack_still_counts():
    """An ACK arriving after the deadline means the write was applied: success,
    not 'timeout' (a caller would otherwise retry an applied setpoint)."""
    frame = build_read_frame(0x15, "b524", "", QQ, _Z1.encode({"Z1DayTemp": 21.5}))

    async def inner():
        link = _Link()
        link.adapter = adapter = _EchoOnly()
        eng = EbusEngine(link.send, link.recv, REG, allow_write=True)
        eng.ACTIVE_DEADLINE = 0.05
        eng.START_MARGIN = 0.0
        task = asyncio.create_task(eng.run())
        wtask = asyncio.create_task(eng.write(_Z1, {"Z1DayTemp": 21.5}, timeout=1.0))
        await asyncio.sleep(0)
        link.inject(encode_symbol(RECEIVED, SYN))
        await asyncio.sleep(0.02)
        assert len(adapter.sent) == len(frame) - 1  # the whole command is out
        await asyncio.sleep(0.1)  # past the deadline, then the ACK arrives
        link.inject(encode_symbol(RECEIVED, ACK))
        await asyncio.wait_for(wtask, 1)
        assert eng.stats.writes == 1 and adapter.starts == 1
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_reconnect_mid_write_fails_the_caller_at_once():
    async def inner():
        link = _Link()
        link.adapter = _EchoOnly()
        eng = EbusEngine(link.send, link.recv, REG, allow_write=True)
        task = asyncio.create_task(eng.run())
        wtask = asyncio.create_task(eng.write(_Z1, {"Z1DayTemp": 21.5}, timeout=5.0))
        await asyncio.sleep(0)
        link.inject(encode_symbol(RECEIVED, SYN))
        await asyncio.sleep(0.02)  # command sent, waiting for the ACK
        eng.attach(link.send, link.recv)  # the supervisor reconnected
        with pytest.raises(WriteUncertain):
            await asyncio.wait_for(wtask, 0.5)  # not the full 5 s
        eng.stop()
        link.inject(b"")
        await asyncio.wait_for(task, 1)

    asyncio.run(inner())


def test_read_refuses_a_write_message():
    eng = EbusEngine(None, None, REG)
    with pytest.raises(ValueError):
        asyncio.run(eng.read(_Z1))


def test_active_read_interleaved():
    async def inner():
        link = _Link()
        msg = BY["Statenumber"]
        cmd = build_read_frame(0x38, "b509", "0dab00", QQ)[len(escape(bytes((QQ,)))):]
        link.adapter = _Adapter(cmd, _slave_wire("1f"))  # 0x1f -> "sommer"
        eng = EbusEngine(link.send, link.recv, REG)
        eng.ACTIVE_DEADLINE = 1.0  # below the request timeout, so the read starts
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
