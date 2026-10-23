"""Bit fields (BIx): packing into a shared byte, value tables, byte advance.

ebusd packs consecutive BIx fields into one byte (the bit position comes from the
name) and moves to the next byte when a non-bit field follows or the position
restarts. The SetMode case is the real eloBlock command payload captured live.
"""
from aioebus.codecs import decode
from aioebus.messages import parse_csv

HDR = "type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment"


def _one(row):
    return parse_csv(HDR + "\n" + row)[0]


def test_single_bit_value_table():
    row = ("r,,,PhaseStatus,,,,b509,0d8800,"
           "phase,,BI0,0=missing;1=present,,,"
           "phase_1,,BI1,0=missing;1=present,,,"
           "phase_2,,BI2,0=missing;1=present,,")
    msg = _one(row)
    # slave byte 0x05 = bits 0 and 2 set
    assert msg.decode(slave="05") == {
        "phase": "present", "phase_1": "missing", "phase_2": "present"
    }


def test_wide_bit_field():
    # BI0:7 reads 7 bits from bit 0
    assert decode("BI0:7", "7f") == 127
    assert decode("BI0:7", "80") == 0    # bit 7 not part of the field
    assert decode("BI1", "02") == 1      # bit 1 set


def test_setmode_real_master_payload():
    row = ("uw,,,SetMode,,,,b510,00,"
           "hcmode,,UCH,0=auto;1=off;2=heat;3=water,,,"
           "flowtempdesired,,D1C,,C,,hwctempdesired,,D1C,,C,,"
           "hwcflowtempdesired,,UCH,,C,,ign,,IGN:1,,,,"
           "disablehc,,BI0,,,,disablehwctapping,,BI1,,,,disablehwcload,,BI2,,,,"
           "ign_1,,IGN:1,,,,"
           "remotecontrolhcpump,,BI0,,,,releasebackup,,BI1,,,,releasecooling,,BI2,,,")
    msg = _one(row)
    # live payload after the '00' id: two flag bytes 0x01 and 0x00, IGN:1 between
    out = msg.decode(master="0000ffffff010000")
    assert out["hcmode"] == "auto"
    assert out["flowtempdesired"] == 0.0
    assert out["hwctempdesired"] is None          # D1C replacement 0xff
    assert out["disablehc"] == 1                   # first flag byte, bit 0
    assert out["disablehwctapping"] == 0
    assert out["disablehwcload"] == 0
    assert out["remotecontrolhcpump"] == 0         # second flag byte, all clear
    assert out["releasecooling"] == 0
