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
