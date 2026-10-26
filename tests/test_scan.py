"""Device scan decode, against real 0704 answers from the reference bus."""
from aioebus.scan import decode_scan

CASES = {
    "b5484d55303009025103": ("Vaillant", "HMU00", "0902", "5103", 902, 5103),
    "b543544c563308088004": ("Vaillant", "CTLV3", "0808", "8004", 808, 8004),
    "b5563332000001219802": ("Vaillant", "V32", "0121", "9802", 121, 9802),
    "b54e4554583301290404": ("Vaillant", "NETX3", "0129", "0404", 129, 404),
}


def test_decode_scan_real():
    for slave_hex, (mf, ident, sw, hw, swn, hwn) in CASES.items():
        r = decode_scan(bytes.fromhex(slave_hex))
        assert (r.manufacturer, r.ident, r.sw, r.hw) == (mf, ident, sw, hw)
        assert (r.sw_num, r.hw_num) == (swn, hwn)


def test_scan_strips_null_padding():
    # V32 id is 3 chars, padded with nulls in the 5-byte field
    assert decode_scan(bytes.fromhex("b5563332000001219802")).ident == "V32"


def test_identification_matches_manual_and_app():
    """b509 24/25/26/27 decode to the Vaillant article number and the full serial.
    The article matches a sensoCOMFORT VRC 720 manual (0010045478); the serials
    match the strings shown in the Vaillant app, character for character."""
    from aioebus.scan import decode_identification
    # f24 has a leading 00 status byte; f25/f26 are ASCII; f27 is 'N#' + padding
    ctlv3 = decode_identification(
        bytes.fromhex("00") + b"21242400", b"100454780", b"953014926", b"N20000000")
    assert ctlv3 is not None
    assert ctlv3.article == "0010045478"  # matches the printed manual
    assert ctlv3.serial == "21242400100454780953014926N2"
    assert ctlv3.year == 2024 and ctlv3.week == 24

    # the two heat pumps share the article (same model), differ in the serial;
    # serials are exactly the strings the Vaillant app shows
    wp1 = decode_identification(
        bytes.fromhex("00") + b"21233400", b"100211220", b"001015434", b"N3<<<<<<<")
    wp2 = decode_identification(
        bytes.fromhex("00") + b"21233400", b"100211220", b"001015550", b"N3<<<<<<<")
    assert wp1.article == wp2.article == "0010021122"
    assert wp1.serial == "21233400100211220001015434N3"
    assert wp2.serial == "21233400100211220001015550N3"

    # a 6-digit article is '<'-padded to ten and stripped back (Vaillant doc example)
    doc = decode_identification(
        bytes.fromhex("00") + b"21040100", b"6304<<<<0", b"001005001", b"N7")
    assert doc.article == "006304"
    assert doc.serial == "210401006304<<<<0001005001N7"
    assert doc.year == 2004 and doc.week == 1

    # not a Vaillant identification -> None
    assert decode_identification(b"\x00garbage", b"x", b"y", b"z") is None
