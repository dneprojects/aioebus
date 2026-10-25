"""Multilingual display text on definitions: the `tr` map round-trips through the
JSON format, and `localized` resolves a language with English/technical fallback."""
from aioebus.convert import from_dicts, merge_languages, to_dicts
from aioebus.messages import FieldDef, MessageDef
from aioebus.translations import localized

_HEADER = ("type,circuit,level,name,comment,qq,zz,pbsb,id,"
           "*name,part,type,divisor/values,unit,comment\n")
_EN = _HEADER + (
    "r,,,FrostOverRideTime,frost delay time,,,b524,020000000300,"
    "value,,UIN,,h,delay before frost protection\n"
    "r,,,HwcParallelLoading,hwc parallel loading,,,b524,020000000a00,"
    "value,,UCH,0=off;1=on,,heating and hot water parallel\n")
# same structure, German comments, rows in a different order (match is by key)
_DE = _HEADER + (
    "r,,,HwcParallelLoading,Parallele Ladung,,,b524,020000000a00,"
    "value,,UCH,0=off;1=on,,Heizen und Warmwasser parallel\n"
    "r,,,FrostOverRideTime,Frostschutz-Verzögerung,,,b524,020000000300,"
    "value,,UIN,,h,Verzögerung bis Frostschutz\n")


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


def test_localized_prefers_language_then_english_then_fallback():
    tr = {"en": "continuous heating", "de": "Durchgehendes Heizen"}
    assert localized(tr, "de", "ContinuousHeating") == "Durchgehendes Heizen"
    assert localized(tr, "fr", "ContinuousHeating") == "continuous heating"  # -> en
    assert localized({"de": "nur Deutsch"}, "de", "Fallback") == "nur Deutsch"
    # neither the requested language nor English present -> technical fallback,
    # not some other language the user did not ask for
    assert localized({"de": "nur Deutsch"}, "en", "Fallback") == "Fallback"
    assert localized(None, "de", "ContinuousHeating") == "ContinuousHeating"
    assert localized({}, "de", "ContinuousHeating") == "ContinuousHeating"


def test_merge_languages_fills_tr_by_key_regardless_of_order():
    msgs = {m.name: m for m in merge_languages({"en": _EN, "de": _DE})}
    frost = msgs["FrostOverRideTime"]
    assert frost.tr == {"en": "frost delay time", "de": "Frostschutz-Verzögerung"}
    assert frost.fields[0].tr == {
        "en": "delay before frost protection", "de": "Verzögerung bis Frostschutz"}
    # the base (en) defines structure and decoding facts
    assert frost.fields[0].type == "UIN" and frost.fields[0].unit == "h"
    assert localized(msgs["HwcParallelLoading"].tr, "de", "x") == "Parallele Ladung"


def test_merge_languages_base_only_has_single_language_tr():
    msgs = {m.name: m for m in merge_languages({"en": _EN})}
    assert msgs["FrostOverRideTime"].tr == {"en": "frost delay time"}
