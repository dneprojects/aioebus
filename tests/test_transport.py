"""Phase 3b enh transport, validated offline: encode real telegrams into a
synthetic adapter byte stream and confirm the passive listener recovers the same
decoded values -- including when the stream is fragmented across reads. The live
socket needs ebusd stopped (single-client adapter) and is not exercised here.
"""
from aioebus.frame import SYN
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry
from aioebus.transport import (
    RECEIVED,
    EnhDecoder,
    PassiveListener,
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


def test_passive_listener_decodes_real_frames():
    listener = PassiveListener(REGISTRY)
    results = []
    for raw, _ in FRAMES:
        results += listener.feed(encode_received_frame(bytes.fromhex(raw)))
    got = [(d.name, list(v.values())[0]) for d, v in results]
    assert got == [exp for _, exp in FRAMES]


def test_decoding_survives_fragmentation():
    # feed the whole stream one byte at a time; stateful decoder must not care
    stream = b"".join(encode_received_frame(bytes.fromhex(r)) for r, _ in FRAMES)
    listener = PassiveListener(REGISTRY)
    results = []
    for i in range(len(stream)):
        results += listener.feed(stream[i : i + 1])
    got = [(d.name, list(v.values())[0]) for d, v in results]
    assert got == [exp for _, exp in FRAMES]
