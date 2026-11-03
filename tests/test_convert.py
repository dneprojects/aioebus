"""The native JSON format: definitions round-trip, display texts included."""
from aioebus.convert import from_dicts, to_dicts
from aioebus.messages import FieldDef, MessageDef


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
