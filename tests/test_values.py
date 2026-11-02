"""Values of a message: labels, keys, numeric types, the control a write allows."""
from aioebus.messages import FieldDef, MessageDef
from aioebus.values import control_of, has_numeric_value, is_number_type, values_of


def msg(direction, *fields, name="M"):
    return MessageDef(direction, "", "", name, "", "", "15", "b524", "00", list(fields))


NUM = FieldDef("value", "", "D2C", None, None, "", "")
ENUM = FieldDef("value", "", "UCH", None, {0: "off", 1: "on"}, "", "")
TEXT = FieldDef("value", "", "STR:5", None, None, "", "")


def test_labels_and_keys():
    lone = values_of(msg("r", NUM, name="FlowTemp"))
    assert [(v.label, v.key) for v in lone] == [("FlowTemp", "flowtemp")]
    multi = values_of(msg("r", FieldDef("temp", "", "D1C", None, None, "", ""),
                          FieldDef("ign", "", "IGN:1", None, None, "", ""),
                          FieldDef("temp_1", "", "D1C", None, None, "", ""), name="Status01"))
    assert [(v.label, v.key) for v in multi] == [
        ("Status01 temp", "status01_temp"), ("Status01 temp_1", "status01_temp_1")]


def test_the_control_a_write_allows():
    assert control_of(msg("w", NUM))[0] == "number"
    assert control_of(msg("w", ENUM))[0] == "select"
    assert control_of(msg("w", TEXT)) is None
    assert control_of(msg("r", NUM)) is None           # reads get no control
    assert control_of(msg("w", NUM, NUM)) is None      # only single-value writes


def test_numeric_types():
    assert has_numeric_value("BCD") and not is_number_type("BCD")
    assert is_number_type("EXP") and is_number_type("UIN") and not has_numeric_value("HEX:2")


def test_passive_writes_get_no_control_and_period_totals():
    from aioebus.values import closes_period, value_key
    assert control_of(msg("uw", NUM)) is None  # sent by others, not by us
    assert closes_period(msg("r", NUM, name="PrEnergySumHcLastMonth"))
    assert not closes_period(msg("r", NUM, name="PrEnergySumHc"))
    v = values_of(msg("r", NUM, name="FlowTemp"))[0]
    assert v.cache_key == value_key(v.msg, "FlowTemp") == ("15", "FlowTemp", "FlowTemp")
