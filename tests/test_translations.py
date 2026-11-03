"""Display texts: stable translation keys and slugs, and the per-language texts
(``tr``) attached from the ebusd CSVs."""
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


def test_texts_of_conditional_variants_come_from_the_first():
    from aioebus.translations import attach_translations
    hdr = "type,circuit,level,name,comment,qq,zz,pbsb,id\n"
    rows = ("[a=1]r,,,Temp,first,,08,b509,0d,value,,UCH,,,\n"
            "[a=2]r,,,Temp,second,,08,b509,0d,value,,UCH,,,\n")
    defs = parse_csv(hdr + rows)
    attach_translations(defs, {"de": parse_csv(hdr + rows.replace("first", "erste").replace(
        "second", "zweite"))})
    assert [d.tr for d in defs] == [{"de": "erste"}, {"de": "zweite"}]


def test_slug():
    assert slug("Mode: Auto/2") == "mode_auto_2"
    assert slug("Mo_Fr") == "mo_fr" and slug("  ") == ""
