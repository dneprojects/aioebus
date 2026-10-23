"""Phase 2: parse real 38.v32.csv rows into the structured model, decode real
captured telegrams into {field: value}, and round-trip through the native JSON.
"""
import json

from aioebus.convert import csv_to_json, from_dicts
from aioebus.messages import parse_csv

# Real rows from our contrib/ebusd-config/38.v32.csv (eloBlock) + defaults.
# The default rows carry the device address (zz=38), as our configs do once the
# filename address is stamped on during conversion.
CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38
*w,elo,,,,,38
r1,,,FlowTemp,d.40 flow temperature,,,b509,0d1800,value,,D2C,,°C,temperature,ign,,IGN:1,,,
r1,,,PumpState,pump status,,,b511,01,ign,,IGN:6,,,,value,,UCH,0=off;1=on;2=overrun;4=hwc,,pump status
r1,,,Statenumber,status number,,,b509,0dab00,value,,UCH,0=kein_bedarf;4=heizen;7=pumpennachlauf;24=warmwasser;31=sommer;34=frostschutz,,
r,,,ModulationDesired,modulation setpoint,,,b509,0d2e00,value,,SIN,10,%,modulation set point
r,,,PartloadHwcKW,d.77 hot water partload,,,b509,0da900,value,,UCH,,kW,storage charging power
"""


def _by_name(msgs):
    return {m.name: m for m in msgs}


def test_parse_and_defaults():
    msgs = _by_name(parse_csv(CSV))
    # circuit comes from the *r,elo default row
    assert msgs["FlowTemp"].circuit == "elo"
    assert msgs["FlowTemp"].zz == "38"  # from the *r default row
    assert msgs["FlowTemp"].pbsb == "b509"
    assert msgs["FlowTemp"].poll_priority == 1
    # two field groups: value + ign
    assert [f.type for f in msgs["FlowTemp"].fields] == ["D2C", "IGN:1"]


def test_decode_real_telegrams():
    msgs = _by_name(parse_csv(CSV))
    # raw decode keys by the CSV field name ("value" is the generic convention)
    assert msgs["FlowTemp"].decode(slave="bd0100") == {"value": 27.8125}
    # labeled decode uses the message name for a lone 'value' field (HA-style)
    assert msgs["FlowTemp"].decode_labeled(slave="bd0100") == {"FlowTemp": 27.8125}
    # PumpState slave: IGN:6 skips the (flow-temp) byte0, UCH at byte6 = 00 -> "off"
    assert msgs["PumpState"].decode_labeled(slave="3700008000ff0000ff") == {"PumpState": "off"}
    # Statenumber 1f -> 31 -> "sommer" via the value table
    assert msgs["Statenumber"].decode_labeled(slave="1f") == {"Statenumber": "sommer"}
    # ModulationDesired SIN with divisor 10: 0x01f4=500 -> 50.0 %
    assert msgs["ModulationDesired"].decode_labeled(slave="f401") == {"ModulationDesired": 50.0}
    assert msgs["PartloadHwcKW"].decode_labeled(slave="0f") == {"PartloadHwcKW": 15}


def test_native_json_roundtrip():
    msgs = parse_csv(CSV)
    data = json.loads(csv_to_json(CSV))
    back = from_dicts(data)
    # same decode result after CSV -> JSON -> model
    orig = _by_name(msgs)["Statenumber"].decode_labeled(slave="04")
    rt = {m.name: m for m in back}["Statenumber"].decode_labeled(slave="04")
    assert orig == rt == {"Statenumber": "heizen"}
    # the FULL dataclasses survive the round-trip (not just a lossy projection),
    # so a dropped field such as zz/qq would fail here
    assert back == msgs
