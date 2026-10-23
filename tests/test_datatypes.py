"""Date/time/float/bit-adjacent datatypes (ebusd datatype.cpp).

Spec vectors are derived from the ebusd builtin-type definitions (byte count,
BCD vs int, byte order, step, replacement). The real-capture block pins the
byte order against regressions using telegrams decoded from the live ctlv3.
"""
from aioebus.codecs import decode


def test_truncated_time():
    assert decode("TTM", "00") == "00:00"
    assert decode("TTM", "8e") == "23:40"   # 142 * 10 min
    assert decode("TTM", "90") is None       # replacement (24:00)


def test_clock_times():
    # hh:mm, integer hours-first / BCD minutes-first / integer minutes-first
    assert decode("HTM", "173b") == "23:59"
    assert decode("BTM", "5923") == "23:59"
    assert decode("VTM", "3b17") == "23:59"
    # hh:mm:ss variants
    assert decode("HTI", "173b3b") == "23:59:59"
    assert decode("BTI", "595923") == "23:59:59"
    assert decode("VTI", "3b3b17") == "23:59:59"
    assert decode("HTM", "ffff") is None


def test_dates_and_weekday():
    assert decode("HDA:3", "1f0c63") == "31.12.2099"   # hex day/month/year
    assert decode("BDA:3", "311299") == "31.12.2099"   # BCD day/month/year
    assert decode("HDA", "1f0c0563") == "31.12.2099"   # 4-byte: weekday skipped
    assert decode("BDY", "00") == "Mon"
    assert decode("BDY", "06") == "Sun"
    assert decode("BDY", "07") is None                 # replacement
    assert decode("HDY", "01") == "Mon"
    assert decode("HDY", "07") == "Sun"


def test_datetime_days_pin():
    assert decode("DTM", "00000000") == "2009-01-01 00:00"
    assert decode("DAY", "0000") == "01.01.1900"
    assert decode("PIN", "0123") == "0123"
    assert decode("PIN", "ffff") is None


def test_float_and_u2l():
    import struct
    assert decode("EXP", struct.pack("<f", 20.5).hex()) == 20.5
    assert decode("EXR", struct.pack(">f", 20.5).hex()) == 20.5
    assert decode("EXP", "0000c07f") is None            # NaN replacement
    assert decode("U2L", "ffff") == 65535               # full range, no replacement


def test_real_captures():
    # from live 15.ctlv3: DisplayedOutsideTemp (EXP), Time (HTI), HwcTimer (HTM)
    assert decode("EXP", "00003e41") == 11.875
    assert decode("HTI", "100736") == "16:07:54"
    assert decode("HTM", "051e") == "05:30"
    assert decode("HTM", "1600") == "22:00"
