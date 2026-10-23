"""Condition prefixes on message rows: parsed off as metadata (not evaluated --
that needs live scan data in Phase 4), instruction rows skipped, and on a pbsb+id
tie an unconditional definition is preferred by the registry.
"""
from aioebus.frame import parse_telegram
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry

HDR = "type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment"


def test_condition_prefix_stripped():
    rows = "\n".join([
        HDR,
        "[id_sw>=200]r,,,ComErrorDelay,,,,b509,0d4300,value,,UIN,,,x",
        "[Scan=1][id_sw>=400]w,,,Foo,,,,b509,0e4300,value,,UIN,,,x",
        "[cond]!load,otherfile,,,,,,,,",            # instruction row -> skipped
        "r,,,Plain,,,,b509,0d4400,value,,UIN,,,x",
    ])
    msgs = {m.name: m for m in parse_csv(rows)}
    assert set(msgs) == {"ComErrorDelay", "Foo", "Plain"}   # !load dropped
    assert msgs["ComErrorDelay"].direction == "r"
    assert msgs["ComErrorDelay"].conditions == ["id_sw>=200"]
    assert msgs["Foo"].direction == "w"
    assert msgs["Foo"].conditions == ["Scan=1", "id_sw>=400"]
    assert msgs["Plain"].conditions is None


def test_registry_prefers_unconditional_on_tie():
    rows = "\n".join([
        HDR,
        "[id_sw>=200]r,,,VariantA,,,38,b509,0d0500,value,,UCH,,,",
        "r,,,VariantB,,,38,b509,0d0500,value,,D2C,,,",
    ])
    reg = MessageRegistry(parse_csv(rows))
    tg = parse_telegram("3138b509030d0500740002bd01bb00")  # synthetic frame id 0d0500
    match = reg.match(tg)
    assert match is not None and match.name == "VariantB"   # unconditional wins
