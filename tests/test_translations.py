"""Translation-key derivation: stable slugs for HA, state maps from value tables."""
from aioebus.messages import parse_csv
from aioebus.translations import ha_strings, translation_key

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


def test_ha_strings_shape():
    strings = ha_strings(MSGS)
    sensors = strings["entity"]["sensor"]
    assert sensors["flowtemp"]["name"] == "FlowTemp"
    # value table becomes a HA state map (slugged keys, label values to translate)
    assert sensors["pumpstate"]["state"] == {"off": "off", "on": "on", "overrun": "overrun"}
    # IGN fields produce no entity
    assert all("ign" not in k for k in sensors)
