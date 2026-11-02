"""Translation-key derivation: stable slugs for HA, state maps from value tables."""
from aioebus.messages import parse_csv
from aioebus.translations import slug, translation_key

CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
*r,elo,,,,,38,,
r1,,,FlowTemp,flow temperature,,,b509,0d1800,value,,D2C,,°C,,ign,,IGN:1,,,
r1,,,PumpState,pump status,,,b511,01,ign,,IGN:6,,,,value,,UCH,0=off;1=on;2=overrun,,
"""
MSGS = parse_csv(CSV)
BY_NAME = {m.name: m for m in MSGS}


def test_translation_key():
    flow = BY_NAME["FlowTemp"]
    # lone 'value' field keys off the message name only (matches decode_labeled)
    assert translation_key(flow, flow.fields[0]) == "flowtemp"
    pump = BY_NAME["PumpState"]
    value_field = [f for f in pump.fields if f.name == "value"][0]
    assert translation_key(pump, value_field) == "pumpstate"


def test_value_fields_skip_ignored_and_nameless():
    pump = BY_NAME["PumpState"]
    assert [f.name for f in pump.value_fields] == ["value"]
    assert slug("Mode: Auto/2") == "mode_auto_2"
