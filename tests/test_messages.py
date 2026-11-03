"""Messages: parse real 38.v32.csv rows into the structured model, decode real
captured telegrams into {field: value}, and round-trip through the native JSON.
"""
import json
import struct

import pytest

from aioebus.convert import csv_to_json, from_dicts
from aioebus.messages import parse_csv

HDR = "type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment"

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


def test_repeated_field_names_get_their_own_keys():
    msg = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
                    "r,hmu,,Status01,,,08,b511,01,temp,,D1C,,,,temp,,D1C,,,,temp,,D1C,,,\n")[0]
    assert [f.name for f in msg.fields] == ["temp", "temp_1", "temp_2"]
    assert msg.decode(slave="283c50") == {"temp": 20.0, "temp_1": 30.0, "temp_2": 40.0}
    again = from_dicts(json.loads(csv_to_json(
        "type,circuit,level,name,comment,qq,zz,pbsb,id\n"
        "r,hmu,,Status01,,,08,b511,01,temp,,D1C,,,,temp,,D1C,,,\n")))[0]
    assert [f.name for f in again.fields] == ["temp", "temp_1"]


def test_definition_with_an_unknown_type_is_skipped(caplog):
    msgs = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
                     "r,,,Odd,,,08,b509,01,value,,SLG,,,\n"
                     "r,,,Fine,,,08,b509,02,value,,UCH,,,\n")
    assert [m.name for m in msgs] == ["Fine"]
    assert "Odd" in caplog.text
    raw = [{"direction": "r", "name": "Odd", "pbsb": "b509", "id": "01",
            "fields": [{"name": "value", "type": "SLG"}]}]
    assert from_dicts(raw) == []


def test_renamed_field_never_takes_another_fields_name():
    msg = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
                    "r,hmu,,S,,,08,b511,01,temp,,UCH,,,,temp,,UCH,,,,temp_1,,UCH,,,\n")[0]
    assert [f.name for f in msg.fields] == ["temp", "temp_2", "temp_1"]


def test_message_encode_builds_master_data():
    msg = parse_csv(HDR + "\n"
                    "w,,,FlowTempDesired,,,38,b509,0e1800,value,,D1C,,°C,\n")[0]
    assert msg.encode({"FlowTempDesired": 30.0}).hex() == "0e18003c"  # id + D1C(30)


def test_message_encode_enum_label():
    msg = parse_csv(HDR + "\n"
                    "w,,,Mode,,,38,b510,00,value,,UCH,0=auto;1=off;2=heat,,\n")[0]
    assert msg.encode({"Mode": "heat"}).hex() == "0002"       # id 00 + UCH 2
    assert msg.encode({"Mode": 1}).hex() == "0001"             # raw int also ok


def test_message_encode_rejects_no_value():
    msg = parse_csv(HDR + "\n"
                    "w,,,Z1DayTemp,,,15,b524,02,temp,,EXP,,°C,\n")[0]
    with pytest.raises(ValueError):
        msg.encode({"Z1DayTemp": None})  # would send the "no value" marker (NaN)


def test_message_encode_accepts_message_name_for_a_lone_field():
    # user definitions name their single field anything, not just "value"
    msg = parse_csv(HDR + "\n"
                    "w,,,Z1DayTemp,,,15,b524,02,temp,,EXP,,°C,\n")[0]
    assert msg.encode({"Z1DayTemp": 21.5}) == bytes.fromhex("02") + struct.pack("<f", 21.5)


DEFAULTS = HDR + """
*r,ehp,,,,,08,B509,0D
*w,ehp,,,,,08,B509,0E,ign,,IGN:1
*uw,bc,,,,,fe,B516,
*u,bc,,,,,fe,B516,01
r,,,FlowTemp,,,,,1800,value,,D2C
w,,,FlowTempDesired,,,,,1800,value,,D1C
r,,,Other,,,,b511,01,value,,UCH
uw,,,Vdatetime,,,,,,time,,BTI
u,,,Outsidetemp,,,,,,value,,D2B
[name]*r,,,,,,,,
r,,,,,,,,0001,value,,UCH
"""


def test_default_rows_apply_as_in_ebusd():
    by = {(m.direction, m.name): m for m in parse_csv(DEFAULTS)}
    flow = by[("r", "FlowTemp")]
    # no PBSB in the row: the default's command, its id as the prefix
    assert (flow.circuit, flow.zz, flow.pbsb, flow.msgid) == ("ehp", "08", "B509", "0D1800")
    # a row with its own PBSB takes no id prefix
    assert (by[("r", "Other")].pbsb, by[("r", "Other")].msgid) == ("b511", "01")
    # the default's fields come first
    desired = by[("w", "FlowTempDesired")]
    assert [f.name for f in desired.fields] == ["ign", "value"]
    assert desired.encode({"value": 30.0}).hex() == "0e1800003c"
    # *u and *uw are two defaults
    assert (by[("uw", "Vdatetime")].pbsb, by[("uw", "Vdatetime")].msgid) == ("B516", "")
    assert (by[("u", "Outsidetemp")].pbsb, by[("u", "Outsidetemp")].msgid) == ("B516", "01")
    assert not any(m.name == "" for m in parse_csv(DEFAULTS))  # nameless rows dropped


def test_default_star_takes_the_value():
    rows = HDR + "\n*r,hc*,,,,,15,b524,\nr,1,,Temp,,,,,02,value,,UCH\nr,,,Bare,,,,,03,value,,UCH\n"
    by = {m.name: m for m in parse_csv(rows)}
    assert (by["Temp"].circuit, by["Bare"].circuit) == ("hc1", "hc")


def test_read_with_arguments_encodes_its_request():
    msg = parse_csv(HDR + "\nr,,,Errorhistory,,,08,b509,11,index,m,UCH,,,,error,,UIN\n")[0]
    assert msg.needs_args and msg.part_of(msg.fields[1]) == "s"
    assert msg.encode({"index": 3}).hex() == "1103"
    assert msg.decode_labeled(master="03", slave="2a00") == {
        "Errorhistory index": 3, "Errorhistory error": 42}
    wild = parse_csv(HDR + "\nw,,,X,,,08,b509,11,ign,,IGN:*,,,,value,,UCH\n")[0]
    assert wild.encode({"value": 1}).hex() == "1101"  # IGN:* adds nothing


def test_label_of():
    from aioebus.messages import label_of
    assert label_of("FlowTemp", "value") == "FlowTemp"
    assert label_of("Status01", "temp") == "Status01 temp"


def test_chained_id_is_skipped_not_fatal():
    from aioebus.passive import MessageRegistry
    rows = HDR + ("\nr,,,YieldLastYear,,,15,b509,0d4300:2;0d4308:2,value,,UIN"
                  "\nr,,,FlowTemp,,,15,b509,0d1800,value,,D2C\n")
    msgs = parse_csv(rows)
    assert [m.name for m in msgs] == ["FlowTemp"]
    bad = msgs[0].__class__(**{**msgs[0].__dict__, "name": "Chain", "msgid": "0d4300:2"})
    reg = MessageRegistry([bad, *msgs])  # e.g. from a JSON file: ignored, not raising
    assert [m.name for m in reg.messages] == ["FlowTemp"]


# where a field without a part lives (ebusd data.cpp): the request for a write,
# the answer for a read -- a passive read too; all of it in the request when
# the destination is a broadcast or a master (nobody answers)
def test_a_passive_read_of_a_device_decodes_its_answer():
    msg = parse_csv(HDR + "\nu,,,YieldTotal,,,08,b509,29bc00,ign,,IGN:2,,,,value,,ULG\n")[0]
    assert [msg.part_of(f) for f in msg.fields] == ["s", "s"]
    assert msg.decode_labeled(b"", bytes.fromhex("000039300000")) == {"YieldTotal": 12345}


def test_parts_to_a_broadcast_or_a_master_are_all_request():
    rows = parse_csv(HDR + "\n"
                     "u,,,Outside,,,fe,b516,01,temp,s,D2C\n"
                     "r,,,ToMaster,,,10,b509,0d,value,,UCH\n"
                     "uw,,,Passive,,,08,b509,0e,value,,UCH\n")
    assert [m.part_of(m.fields[0]) for m in rows] == ["m", "m", "m"]


def test_a_write_value_of_the_answer_is_refused():
    msg = parse_csv(HDR + "\nw,,,Clearerrorhistory,,,08,b503,0201,cleared,s,UCH,0=no;1=yes\n")[0]
    assert msg.request_fields() == []
    with pytest.raises(ValueError, match="not part of the request"):
        msg.encode({"cleared": "no"})


def test_a_constant_is_written_by_itself_and_is_no_value():
    msg = parse_csv(HDR + "\n"
                    "w,,,EnableTest,,,08,b514,0515,ignore,,HEX:3,=03FFFF,,,"
                    "slot,,UCH,,,,wconst,,U2L,=65535,,\n")[0]
    assert [f.name for f in msg.value_fields] == ["slot"]
    assert msg.encode({"slot": 2}).hex() == "051503ffff02ffff"
    assert msg.decode(bytes.fromhex("03ffff020000")) == {"slot": 2}  # unverified


def test_a_verified_constant_must_match():
    msg = parse_csv(HDR + "\nr,,,Checked,,,08,b509,0d,kind,,UCH,==3,,,value,,UCH\n")[0]
    assert msg.decode(b"", bytes.fromhex("0307")) == {"value": 7}
    with pytest.raises(ValueError, match="is not 3"):
        msg.decode(b"", bytes.fromhex("0407"))


def test_constants_survive_the_json_format():
    msg = parse_csv(HDR + "\nr,,,Checked,,,08,b509,0d,kind,,UCH,==3,,,value,,UCH\n")[0]
    back = from_dicts(json.loads(csv_to_json(HDR + "\nr,,,Checked,,,08,b509,0d,kind,,UCH,==3,,,value,,UCH\n")))[0]
    assert (back.fields[0].const, back.fields[0].verify) == ("3", True) == (
        msg.fields[0].const, msg.fields[0].verify)


def test_a_value_table_is_looked_up_by_the_raw_value():
    msg = parse_csv(HDR + "\n"
                    "r,,,Mode,,,08,b509,0d,mode,,UCH,1=on;255=off,,,"
                    "level,,SCH,1=low;255=none,,\n")[0]
    # the table comes before the replacement value, and a signed type's raw byte
    assert msg.decode(b"", bytes.fromhex("ffff")) == {"mode": "off", "level": "none"}
    assert msg.decode(b"", bytes.fromhex("0705")) == {"mode": 7, "level": 5}  # raw number
    write = parse_csv(HDR + "\nw,,,Level,,,08,b509,0e,level,,SCH,1=low;255=none,,\n")[0]
    assert write.encode({"level": "none"}).hex() == "0eff"


def test_negative_and_formatted_constants():
    msg = parse_csv(HDR + "\n"
                    "w,,,Neg,,,08,b509,0e,k,,SCH,=-1,,,value,,UCH\n"
                    "r,,,At,,,08,b509,0f,t,,HTM,==10:00,,,value,,UCH\n")
    assert msg[0].encode({"value": 5}).hex() == "0eff05"
    assert msg[1].decode(b"", bytes.fromhex("0a0007")) == {"value": 7}
    with pytest.raises(ValueError, match="is not 10:00"):
        msg[1].decode(b"", bytes.fromhex("0b0007"))


def test_destination_address_and_id_bytes():
    rows = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
                     "r,,,A,,,15,b509,0d1800,value,,UCH,,,\n"
                     "r,,,B,,,,b509,0d1900,value,,UCH,,,\n"
                     "r,,,C,,,x8,b509,,value,,UCH,,,\n")
    assert [m.address for m in rows] == [0x15, None, None]
    assert rows[0].id_bytes == bytes.fromhex("0d1800") and rows[2].id_bytes == b""


def test_value_fields_skip_ignored_and_nameless():
    pump = parse_csv("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
                     "r1,,,PumpState,,,38,b511,01,ign,,IGN:6,,,,value,,UCH,0=off;1=on,,"
                     ",,,UCH,,,\n")[0]
    assert [f.name for f in pump.value_fields] == ["value"]
