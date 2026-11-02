"""Enhanced-adapter transport, validated offline: encode real telegrams into a
synthetic adapter byte stream and confirm the engine recovers the same decoded
values -- including when the stream is fragmented across reads.
"""
from aioebus.frame import SYN
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry
from aioebus.transport import (
    RECEIVED,
    EnhDecoder,
    encode_received_frame,
    encode_symbol,
)

CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38,,
r1,,,FlowTemp,flow temperature,,,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,
r,,,ModulationDesired,modulation setpoint,,,b509,0d2e00,value,,SIN,10,%,
r1,,,Statenumber,status number,,,b509,0dab00,value,,UCH,0=kein_bedarf;31=sommer,,
"""
REGISTRY = MessageRegistry(parse_csv(CSV))

FRAMES = [
    ("3138b509030d1800740003910100bb00", ("FlowTemp", 25.0625)),
    ("3138b509030d2e004e000271022b00", ("ModulationDesired", 62.5)),
    ("3138b509030dab001f00011f8400", ("Statenumber", "sommer")),
]


def test_symbol_roundtrip():
    dec = EnhDecoder()
    for value in (0x00, 0x37, 0x7F, 0x80, 0xA9, SYN, 0xFF):
        assert dec.feed(encode_symbol(RECEIVED, value)) == [(RECEIVED, value)]
    # a byte >= 0x80 really does take the two-byte form; < 0x80 is short form
    assert len(encode_symbol(RECEIVED, 0xAA)) == 2
    assert len(encode_symbol(RECEIVED, 0x37)) == 1


def _engine_decodes(chunks: list[bytes]) -> list[tuple[str, object]]:
    """Feed adapter chunks to an engine (no writes); the values it decoded."""
    import asyncio

    from aioebus.engine import EbusEngine

    got: list[tuple[str, object]] = []

    async def inner():
        queue: asyncio.Queue = asyncio.Queue()
        for chunk in [*chunks, b""]:
            queue.put_nowait(chunk)

        async def send(_data: bytes) -> None:
            raise AssertionError("a passive engine sends nothing")

        eng = EbusEngine(send, queue.get, REGISTRY,
                         on_value=lambda m, v: got.append((m.name, next(iter(v.values())))))
        await asyncio.wait_for(eng.run(), 2)

    asyncio.run(inner())
    return got


def test_engine_decodes_real_frames():
    stream = [encode_received_frame(bytes.fromhex(raw)) for raw, _ in FRAMES]
    assert _engine_decodes(stream) == [exp for _, exp in FRAMES]


def test_decoding_survives_fragmentation():
    # feed the whole stream one byte at a time; the stateful decoder must not care
    stream = b"".join(encode_received_frame(bytes.fromhex(r)) for r, _ in FRAMES)
    assert _engine_decodes([stream[i:i + 1] for i in range(len(stream))]) == [
        exp for _, exp in FRAMES]


def test_short_form_byte_after_an_orphan_first_half_is_kept():
    # a desynced first half followed by short-form bus bytes: ebusd keeps them
    events = EnhDecoder().feed(bytes((0xC4, 0x45, 0x31, 0x32)))
    assert events == [(RECEIVED, 0x45), (RECEIVED, 0x31), (RECEIVED, 0x32)]
