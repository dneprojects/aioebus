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
