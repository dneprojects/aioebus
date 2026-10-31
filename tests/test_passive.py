"""Phase 3 closed loop: real wire frames captured from the eloBlock, fed through
the frame parser + message registry, decode to the same named values ebusd gives.
No bus access -- pure replay of captured telegrams.
"""
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry, decode_raw

CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38,,
r1,,,FlowTemp,flow temperature,,,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,
r1,,,PumpState,pump status,,,b511,01,ign,,IGN:6,,,,value,,UCH,0=off;1=on;2=overrun;4=hwc,,
r1,,,Statenumber,status number,,,b509,0dab00,value,,UCH,0=kein_bedarf;4=heizen;31=sommer,,
r,,,ModulationDesired,modulation setpoint,,,b509,0d2e00,value,,SIN,10,%,
r,,,PartloadHwcKW,hot water partload,,,b509,0da900,value,,UCH,,kW,
"""

REGISTRY = MessageRegistry(parse_csv(CSV))

# (raw wire frame, expected labelled decode) -- all captured from the live bus
CASES = [
    ("3138b509030d1800740003910100bb00", {"FlowTemp": 25.0625}),
    ("3138b509030dab001f00011f8400", {"Statenumber": "sommer"}),
    ("3138b509030d2e004e000271022b00", {"ModulationDesired": 62.5}),
    ("3138b509030da90000f000010f9400", {"PartloadHwcKW": 15}),
    ("1038b51101012900093200008000ff0000ff4100", {"PumpState": "off"}),
]


def test_passive_decode_real_frames():
    for raw, expected in CASES:
        result = decode_raw(REGISTRY, raw)
        assert result is not None, raw
        msgdef, values = result
        assert values == expected, (raw, values)


def test_unanswered_read_has_no_value():
    # another master's FlowTemp read, ACKed but never answered (the capture ends
    # after the ACK): nothing to decode, so no None value may be reported
    assert decode_raw(REGISTRY, "3138b509030d18007400") is None


def test_unknown_command_returns_none():
    # a valid frame whose command/id isn't in the registry decodes to None
    assert decode_raw(REGISTRY, "31fe07fe0035") is None
