"""Active read-frame construction (Phase 4 read side, offline)."""
from aioebus.active import build_read, build_read_frame
from aioebus.frame import parse_telegram
from aioebus.messages import parse_csv


def test_build_read_frame_roundtrips():
    # FlowTemp read from the eloBlock @38: known-good wire bytes
    wire = build_read_frame(0x38, "b509", "0d1800", qq=0x31)
    assert wire.hex() == "3138b509030d180074"
    tg = parse_telegram(wire + b"\x00")  # append slave ACK so it parses as MM
    assert (tg.qq, tg.zz, tg.pbsb) == (0x31, 0x38, "b509")
    assert tg.data.hex() == "0d1800"
    assert tg.crc_ok


def test_build_read_from_message():
    csv = (
        "type,circuit,level,name,comment,qq,zz,pbsb,id\n"
        "r1,elo,,FlowTemp,,,38,b509,0d1800\n"
    )
    msg = parse_csv(csv)[0]
    assert build_read(msg, qq=0x31).hex() == "3138b509030d180074"


def test_escaping_in_master_data():
    # an id byte 0xaa must be escaped on the wire (aa -> a9 01) and the CRC still
    # verifies, because CRC is computed over the escaped stream
    wire = build_read_frame(0x15, "b509", "0daa00", qq=0x31)
    assert "a901" in wire.hex()  # 0xaa escaped
    tg = parse_telegram(wire + b"\x00")
    assert tg.data.hex() == "0daa00"
    assert tg.crc_ok
