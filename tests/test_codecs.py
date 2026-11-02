"""Conformance tests: decode REAL telegrams captured from the live bus this
session (Vaillant eloBlock @38, VR 71 @26, sensoNET @f6) and check against the
values we verified independently (app, cross-reads). This is the golden-data net
that lets us port aggressively and catch regressions.
"""
import struct

import pytest

from aioebus.codecs import decode, encode


def test_real_captures():
    # eloBlock FlowTemp 0d1800 slave 'bd0100' -> D2C (0x01bd/16)
    assert decode("D2C", "bd01") == 27.8125
    # PartloadHwcKW 0da900 slave '0f' -> UCH
    assert decode("UCH", "0f") == 15
    # eloBlock b511/01 status byte0 '37' -> flow temp as D1C (/2); matched FlowTemp
    assert decode("D1C", "37") == 27.5
    # VR 71 SensorData1 field1 '1402' -> D2C = "Vorlauftemperatur Anlage" (~app 34.5)
    assert decode("D2C", "1402") == 33.25
    # Statenumber 0dab00 slave '1f' -> UCH 31 (= "sommer" via values map)
    assert decode("UCH", "1f") == 31
    # ProbeReg3a03 slave '2d00' -> UIN 45
    assert decode("UIN", "2d00") == 45
    # sensoNET identification ID 'NETX3' -> STR:5
    assert decode("STR:5", "4e45545833") == "NETX3"
    # eloBlock PowerValue 0daa00 slave '090264186418' -> HEX:6 (raw, undecoded)
    assert decode("HEX:6", "090264186418") == "09 02 64 18 64 18"
    # eloBlock WaterPressure 0d0200 slave '8c09' -> FLT (2-byte /1000) = bar
    assert decode("FLT", "8c09") == 2.444


def test_invalid_sentinels():
    assert decode("UCH", "ff") is None
    assert decode("UIN", "ffff") is None
    assert decode("SIN", "0080") is None
    assert decode("D2C", "0080") is None


def test_divisor_column():
    # ModulationDesired 0d2e00 is SIN with divisor 10 in the CSV -> e.g. 0x01f4=500 -> 50.0
    assert decode("SIN", "f401", divisor=10) == 50.0
    # negative divisor multiplies
    assert decode("UCH", "05", divisor=-100) == 500


def test_float_divisor_column():
    # EXP/EXR (IEEE binary32) must honor the CSV divisor like the integer types.
    # Real HMU b516/10 StatEnvironmentEnergySumHc: trailing float -> kWh (/1000).
    assert decode("EXP", "372f9f4b") == 20864622.0  # raw float, no divisor
    assert decode("EXP", "372f9f4b", divisor=1000) == 20864.622
    assert decode("EXP", "372f9f4b", divisor=-2) == 41729244.0  # negative multiplies
    # big-endian EXR (same bytes reversed) applies it too
    assert decode("EXR", "4b9f2f37", divisor=1000) == 20864.622


def test_signed_and_remainder():
    assert decode("SCH", "fe") == -2
    assert decode("STR:*", "466268") == "Fbh"  # variable-length zone short name


def test_encode_numeric_roundtrips():
    assert encode("UCH", 15) == bytes.fromhex("0f")
    assert encode("D1C", 30.0) == bytes.fromhex("3c")
    assert encode("D2C", 27.8125) == bytes.fromhex("bd01")
    assert encode("SIN", 50.0, divisor=10) == bytes.fromhex("f401")
    for spec, raw in [("UCH", "0f"), ("D1C", "3c"), ("D2C", "bd01")]:
        assert encode(spec, decode(spec, raw)) == bytes.fromhex(raw)


def test_encode_rejects_non_numeric():
    with pytest.raises(NotImplementedError):
        encode("STR:3", "abc")


def test_encode_rounds_half_away_from_zero():
    assert encode("D1C", 20.25) == bytes((41,))  # 40.5 -> 41, i.e. 20.5 °C
    assert encode("D2C", -0.03125) == (-1).to_bytes(2, "little", signed=True)


def test_encode_rejects_out_of_range_and_replacement():
    with pytest.raises(ValueError):
        encode("SCH", -200)  # would wrap to 56 without the range check
    with pytest.raises(ValueError):
        encode("UCH", 256)
    with pytest.raises(ValueError):
        encode("UCH", 255)  # 0xFF is UCH's "no value"
    with pytest.raises(ValueError):
        encode("D2C", -2048)  # raw 0x8000, the replacement
    assert encode("SCH", -127) == bytes.fromhex("81")


def test_encode_exp_float():
    assert encode("EXP", 21.5) == struct.pack("<f", 21.5)
    assert decode("EXP", encode("EXP", 21.5)) == 21.5
    assert decode("EXP", encode("EXP", 1.5, divisor=1000), divisor=1000) == 1.5
    with pytest.raises(ValueError):
        encode("EXP", float("nan"))




def test_encode_rejects_non_finite():
    with pytest.raises(ValueError):
        encode("UCH", float("inf"))


def test_text_ends_at_nul_and_shows_controls_as_ebusd():
    assert decode("STR:8", b"Bad\x00xxxx") == "Bad"
    assert decode("STR:4", b"a\x07b\x7f") == "a b?"
    assert decode("STR:6", "4bfc63686520") == "Küche"  # Latin-1 letters stay


def test_wildcard_lengths_take_the_rest():
    assert decode("HEX:*", "010203") == "01 02 03"
    assert decode("IGN:*", "0102") is None
    from aioebus.codecs import make_codec
    assert make_codec("HEX:*").length == -1 and make_codec("IGN:*").length == -1
