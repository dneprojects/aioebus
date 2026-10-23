"""Frame-layer conformance: every telegram here was captured verbatim from the
live Vaillant bus (ebusd SQLite log) and its expected fields are ebusd's own
decoded columns. `tools/validate_frame_db.py` runs the same checks across all
285k telegrams; this file is the offline subset that runs without the DB.
"""
from aioebus.frame import crc8, escape, parse_telegram, unescape


def test_crc_known_master():
    # master frame QQ ZZ PB SB NN DD of a real eloBlock b510, CRC 0xBF on the wire
    assert crc8(bytes.fromhex("1038b51009000000ffffff010000")) == 0xBF


def test_escape_roundtrip():
    for raw in ("00", "aa", "a9", "aab5a9", "0d a900".replace(" ", "")):
        b = bytes.fromhex(raw)
        assert unescape(escape(b)) == b
    # concrete wire escapes seen on the bus
    assert unescape(bytes.fromhex("a900")) == bytes.fromhex("a9")
    assert unescape(bytes.fromhex("a901")) == bytes.fromhex("aa")


def test_plain_master_slave():
    tg = parse_telegram("1038b51009000000ffffff010000bf0001019a00")
    assert (tg.qq, tg.zz, tg.pbsb, tg.kind) == (0x10, 0x38, "b510", "MS")
    assert tg.data.hex() == "000000ffffff010000"
    assert tg.crc == 0xBF
    assert tg.slave_data.hex() == "01"
    assert tg.crc_ok


def test_broadcast_has_no_answer():
    tg = parse_telegram("31fe07fe0035")
    assert tg.kind == "BC"
    assert (tg.zz, tg.nn, tg.crc) == (0xFE, 0, 0x35)
    assert tg.slave_data == b""
    assert tg.crc_ok


def test_escaped_master_data():
    # DD carries an A9 -> escaped A9 00 on the wire; CRC is over the escaped form
    tg = parse_telegram("3138b509030da90000f000010f9400")
    assert tg.data.hex() == "0da900"
    assert tg.crc == 0xF0
    assert tg.crc_ok


def test_escaped_slave_data():
    tg = parse_telegram("3118b5160114b8000901a90006ad44000000007700")
    assert tg.data.hex() == "14"
    assert tg.slave_data.hex() == "01a906ad4400000000"
    assert tg.slave_crc == 0x77
    assert tg.crc_ok


def test_slave_nak_retransmit():
    # slave answer NAKed (ACK 0xFF) and resent; we keep the accepted copy
    tg = parse_telegram(
        "1038b51101012900097000008000ff0000ff54ff093900008000ff0000ff5400"
    )
    assert tg.slave_data.hex() == "3900008000ff0000ff"
    assert tg.slave_ack == 0x00
    assert tg.crc_ok


def test_master_nak_retransmit():
    # first master attempt garbled (PB read as 0x28), NAKed, resent correctly
    tg = parse_telegram(
        "f115280302000180fff115b50302000180000affffffffffffffffffff0f00"
    )
    assert (tg.qq, tg.zz, tg.pbsb) == (0xF1, 0x15, "b503")
    assert tg.data.hex() == "0001"
    assert tg.slave_data.hex() == "ffffffffffffffffffff"
    assert tg.crc_ok
