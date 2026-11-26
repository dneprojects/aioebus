"""Acknowledgments read passively: what the frame layer reports, how a fault is
named, the meter's rate and what the engine does with each fault."""
import asyncio

from aioebus.acks import ANSWER_REFUSED, NO_ACK, REFUSED, REPEATED, AckFault, fault_of
from aioebus.engine import EbusEngine
from aioebus.frame import parse_telegram
from aioebus.messages import parse_csv
from aioebus.meter import BusMeter
from aioebus.passive import MessageRegistry
from aioebus.transport import encode_received_frame

# 31 reads FlowTemp of 38 (CRC 74); the answer 03 910100 (CRC bb)
HEAD = "3138b509030d180074"
ANSWER = "03910100bb"
CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38
r1,,,FlowTemp,,,,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,
"""
REG = MessageRegistry(parse_csv(CSV))


def test_clean_exchange_has_no_fault():
    tg = parse_telegram(HEAD + "00" + ANSWER + "00")
    assert (tg.master_naks, tg.slave_naks) == (0, 0)
    assert fault_of(tg) is None
    assert fault_of(parse_telegram("31fe07fe0035")) is None  # a broadcast


def test_refused_for_good_is_parsed_not_dropped():
    once = parse_telegram(HEAD + "ff")  # NAK, no resend
    twice = parse_telegram(HEAD + "ff" + HEAD + "ff")  # NAK, resend NAKed too
    for tg, naks in ((once, 1), (twice, 2)):
        assert (tg.master_ack, tg.master_naks, tg.slave_data) == (0xFF, naks, b"")
        assert tg.crc_ok and fault_of(tg) == REFUSED


def test_taken_on_the_resend_is_a_repeat():
    tg = parse_telegram(HEAD + "ff" + HEAD + "00" + ANSWER + "00")
    assert (tg.master_naks, tg.master_ack, tg.slave_data.hex()) == (1, 0x00, "910100")
    assert fault_of(tg) == REPEATED


def test_no_acknowledgment_and_a_refused_answer():
    assert fault_of(parse_telegram(HEAD)) == NO_ACK
    tg = parse_telegram(HEAD + "00" + "03910100bc" + "ff" + ANSWER + "00")
    assert tg.slave_naks == 1 and tg.slave_data.hex() == "910100"
    assert fault_of(tg) == ANSWER_REFUSED


def test_lost_means_the_command_did_not_arrive():
    def rec(fault):
        return AckFault(0.0, 0x10, 0x26, "b510", b"\x01", fault)
    assert rec(NO_ACK).lost and rec(REFUSED).lost
    assert not rec(REPEATED).lost and not rec(ANSWER_REFUSED).lost
    assert rec(REFUSED).as_dict()["zz"] == "26"


def test_meter_counts_faults_per_device_and_lost_ones():
    m = BusMeter()
    m.symbol(0xAA, 0.0)
    m.ack_fault(0x26, True, 10.0)
    m.ack_fault(0x26, False, 20.0)
    m.ack_fault(0x0B, True, 30.0)  # an address without a device
    assert m.ack_faults_per_minute(60.0) == 3.0
    assert m.ack_faults_per_minute(60.0, {0x26}) == 2.0
    assert m.ack_faults_per_minute(60.0, {0x26}, lost_only=True) == 1.0
    assert m.ack_faults_per_minute(200.0) == 0.0  # all older than a minute


def _run(frames: list[str], on_fault=None):
    """Feed frames to a passive engine; its faults and the values it decoded."""
    got: list[tuple[str, object]] = []

    async def inner():
        queue: asyncio.Queue = asyncio.Queue()
        for raw in frames:
            queue.put_nowait(encode_received_frame(bytes.fromhex(raw)))
        queue.put_nowait(b"")

        async def send(_data: bytes) -> None:
            raise AssertionError("a passive engine sends nothing")

        eng = EbusEngine(send, queue.get, REG, on_ack_fault=on_fault,
                         on_value=lambda m, v: got.append((m.name, next(iter(v.values())))))
        await asyncio.wait_for(eng.run(), 2)
        return eng

    return asyncio.run(inner()), got


def test_engine_records_each_fault_and_keeps_decoding():
    seen: list[AckFault] = []
    eng, got = _run([HEAD + "ff",                                  # refused
                     HEAD + "ff" + HEAD + "00" + ANSWER + "00",    # taken on the resend
                     HEAD,                                         # not acknowledged
                     HEAD + "00" + ANSWER + "00"], seen.append)    # clean
    assert [f.fault for f in seen] == [REFUSED, REPEATED, NO_ACK]
    assert [f.fault for f in eng.ack_faults] == [REFUSED, REPEATED, NO_ACK]
    assert seen[0].message == "FlowTemp" and seen[0].zz == 0x38 and seen[0].lost
    # the repeat and the clean read carry the value; the refused one does not
    assert got == [("FlowTemp", 25.0625), ("FlowTemp", 25.0625)]
    assert eng.meter.ack_faults_per_minute(eng.stats.last_telegram, lost_only=True) is not None
    assert len(eng.meter._ack_faults) == 3


def test_a_garbled_master_part_is_no_fault_and_a_failing_callback_is_harmless():
    def boom(_fault):
        raise RuntimeError("consumer bug")
    eng, got = _run(["3138b509030d180075ff",  # wrong CRC: who it was for is unknown
                     HEAD,                    # a fault whose callback raises
                     HEAD + "00" + ANSWER + "00"], boom)
    assert [f.fault for f in eng.ack_faults] == [NO_ACK]
    assert got == [("FlowTemp", 25.0625)]


# -- our own telegrams: the active driver tells what went wrong ---------------

import pytest  # noqa: E402

from aioebus.active import ReadDriver, ReadError, build_read_frame  # noqa: E402
from aioebus.transport import (  # noqa: E402
    RECEIVED,
    SEND,
    START,
    STARTED,
    EnhDecoder,
    encode_symbol,
)

GOOD = bytes.fromhex("00" + ANSWER)  # ACK + the answer
BAD = bytes.fromhex("0003910100bc")  # ACK + the answer with a wrong CRC


def _drive(*after: bytes) -> ReadDriver:
    """Our FlowTemp read; ``after[i]`` arrives once attempt i went out."""
    full = build_read_frame(0x38, "b509", "0d1800", qq=0x31)
    drv = ReadDriver(0x38, "b509", "0d1800", 0x31)
    drv.start()
    drv.feed(encode_symbol(STARTED, 0x31))
    for i, reply in enumerate(after):
        for b in full[1:] if i == 0 else full:  # a repeat starts again at QQ
            drv.feed(encode_symbol(RECEIVED, b))  # the echo
        for b in reply:
            if not drv.done:
                drv.feed(encode_symbol(RECEIVED, b))
    return drv


def test_own_telegram_faults():
    assert _drive(GOOD).ack_fault is None
    assert _drive(b"\xff", GOOD).ack_fault == REPEATED
    assert _drive(b"\xff", b"\xff").ack_fault == REFUSED
    assert _drive(b"\xaa").ack_fault == NO_ACK  # the bus went idle instead
    nakd = _drive(BAD + b"\xff" + GOOD[1:])  # we NAK the answer, it comes again
    assert nakd.error is None and nakd.ack_fault == ANSWER_REFUSED
    collided = ReadDriver(0x38, "b509", "0d1800", 0x31)
    collided.start()
    collided.feed(encode_symbol(STARTED, 0x31))
    collided.feed(encode_symbol(RECEIVED, 0x99))  # a collision: nothing was due
    assert collided.done and collided.ack_fault is None


class _OwnAdapter:
    """Echoes what we send; after each full attempt of our read, the next reply."""

    def __init__(self, *replies: bytes) -> None:
        self.full = build_read_frame(0x38, "b509", "0d1800", qq=0x31)
        self.replies = list(replies)
        self.sent = 0
        self.dec = EnhDecoder()
        self.queue: asyncio.Queue = asyncio.Queue()

    async def send(self, data: bytes) -> None:
        out = bytearray()
        for code, byte in self.dec.feed(data):
            if code == START:
                out += encode_symbol(STARTED, byte)
                self.sent = 1
            elif code == SEND and self.replies:
                out += encode_symbol(RECEIVED, byte)
                self.sent += 1
                if self.sent == len(self.full):
                    self.sent = 0
                    for b in self.replies.pop(0):
                        out += encode_symbol(RECEIVED, b)
        if out:
            self.queue.put_nowait(bytes(out))

    async def recv(self) -> bytes:
        return await self.queue.get()


def test_engine_records_its_own_faults_of_known_registers_only():
    seen: list[AckFault] = []
    msg = parse_csv(CSV)[0]

    async def inner():
        link = _OwnAdapter(b"\xff", GOOD, b"\xaa")
        eng = EbusEngine(link.send, link.recv, REG, qq=0x31, on_ack_fault=seen.append)
        task = asyncio.create_task(eng.run())
        syn = encode_symbol(RECEIVED, 0xAA)  # the bus is idle: we may start
        read = asyncio.create_task(eng.read(msg))
        await asyncio.sleep(0.01)
        link.queue.put_nowait(syn)
        assert await read == {"FlowTemp": 25.0625}  # taken on the resend
        # a raw read, as a scan probing an empty address does: no fault of ours
        probe = asyncio.create_task(eng.read_raw(0x38, "b509", "0d1800", timeout=5))
        await asyncio.sleep(0.01)
        link.queue.put_nowait(syn)
        await asyncio.sleep(0.05)
        link.queue.put_nowait(syn)  # the bus goes idle without an ACK
        with pytest.raises(ReadError, match="unexpected symbol waiting for command ACK"):
            await probe  # it was not acknowledged, but is not recorded
        eng.stop()
        link.queue.put_nowait(b"")
        await asyncio.wait_for(task, 2)
        return eng

    eng = asyncio.run(inner())
    assert [(f.fault, f.own, f.message, f.qq) for f in seen] == [
        (REPEATED, True, "FlowTemp", 0x31)]
    assert len(eng.meter._ack_faults) == 1


def test_a_command_lost_again_and_again_has_a_streak_until_it_arrives():
    seen: list[AckFault] = []
    clean = HEAD + "00" + ANSWER + "00"
    other = "3138b509030d2e004e000271022b00"  # another command to 38 arrives
    eng, _got = _run([HEAD, HEAD + "ff", other, HEAD,  # lost 3x in a row
                      clean,                           # arrives: the run ends
                      HEAD,                            # lost once more: 1
                      HEAD + "ff" + clean],            # taken on the resend: 0
                     seen.append)
    assert [(f.fault, f.streak) for f in seen] == [
        (NO_ACK, 1), (REFUSED, 2), (NO_ACK, 3), (NO_ACK, 1), (REPEATED, 0)]
    assert eng.lost_streaks == {}  # the last one arrived on the resend


def test_our_own_lost_read_starts_a_streak_that_ends_when_it_arrives():
    seen: list[AckFault] = []
    msg = parse_csv(CSV)[0]

    async def inner():
        link = _OwnAdapter(b"\xaa", GOOD)
        eng = EbusEngine(link.send, link.recv, REG, qq=0x31, on_ack_fault=seen.append)
        task = asyncio.create_task(eng.run())
        syn = encode_symbol(RECEIVED, 0xAA)
        first = asyncio.create_task(eng.read(msg))
        await asyncio.sleep(0.01)
        link.queue.put_nowait(syn)
        with pytest.raises(ReadError):
            await first  # not acknowledged
        streaks = dict(eng.lost_streaks)
        second = asyncio.create_task(eng.read(msg))
        await asyncio.sleep(0.01)
        link.queue.put_nowait(syn)
        assert await second == {"FlowTemp": 25.0625}
        eng.stop()
        link.queue.put_nowait(b"")
        await asyncio.wait_for(task, 2)
        return eng, streaks

    eng, streaks = asyncio.run(inner())
    assert [(f.fault, f.streak, f.own) for f in seen] == [(NO_ACK, 1, True)]
    assert streaks == {(0x31, 0x38, "b509", "FlowTemp"): 1}
    assert eng.lost_streaks == {}  # it arrived: the run ended
