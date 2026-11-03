"""Display texts: stable translation keys and slugs, and the per-language
texts (``tr``) definitions carry through the JSON format."""
from aioebus.convert import from_dicts, to_dicts
from aioebus.messages import FieldDef, MessageDef, parse_csv
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


def _msg() -> MessageDef:
    return MessageDef(
        direction="r", circuit="", level="", name="ContinuousHeating",
        comment="continuous heating", qq="", zz="15", pbsb="b524", msgid="020000000200",
        fields=[FieldDef("value", "", "EXP", None, None, "°C", "continuous heating",
                         tr={"en": "continuous heating", "de": "Durchgehendes Heizen"})],
        tr={"en": "continuous heating", "de": "Durchgehendes Heizen"},
    )


def test_tr_round_trips_through_json():
    msg = _msg()
    back = from_dicts(to_dicts([msg]))[0]
    assert back == msg  # full dataclass equality, incl. tr on message and field
    assert back.tr == {"en": "continuous heating", "de": "Durchgehendes Heizen"}
    assert back.fields[0].tr == {"en": "continuous heating", "de": "Durchgehendes Heizen"}


def test_tr_absent_stays_none():
    msg = MessageDef(direction="r", circuit="", level="", name="X", comment="",
                     qq="", zz="15", pbsb="b509", msgid="00", fields=[])
    back = from_dicts(to_dicts([msg]))[0]
    assert back.tr is None
    assert "tr" not in to_dicts([msg])[0]


def test_texts_of_conditional_variants_come_from_the_first():
    from aioebus.convert import attach_translations
    hdr = "type,circuit,level,name,comment,qq,zz,pbsb,id\n"
    rows = ("[a=1]r,,,Temp,first,,08,b509,0d,value,,UCH,,,\n"
            "[a=2]r,,,Temp,second,,08,b509,0d,value,,UCH,,,\n")
    defs = parse_csv(hdr + rows)
    attach_translations(defs, {"de": parse_csv(hdr + rows.replace("first", "erste").replace(
        "second", "zweite"))})
    assert [d.tr for d in defs] == [{"de": "erste"}, {"de": "zweite"}]
