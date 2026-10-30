"""Conformance tests: decode REAL telegrams captured from the live bus this
session (Vaillant eloBlock @38, VR 71 @26, sensoNET @f6) and check against the
values we verified independently (app, cross-reads). This is the golden-data net
that lets us port aggressively and catch regressions.
"""
from aioebus.codecs import decode


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
