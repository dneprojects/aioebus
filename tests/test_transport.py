"""Enhanced-adapter transport: the symbol forms in both directions and the
stateful decoder (real frames through the engine: test_engine.py)."""
from aioebus.frame import SYN
from aioebus.transport import (
    RECEIVED,
    EnhDecoder,
    encode_symbol,
)


def test_symbol_roundtrip():
    dec = EnhDecoder()
    for value in (0x00, 0x37, 0x7F, 0x80, 0xA9, SYN, 0xFF):
        assert dec.feed(encode_symbol(RECEIVED, value)) == [(RECEIVED, value)]
    # a byte >= 0x80 really does take the two-byte form; < 0x80 is short form
    assert len(encode_symbol(RECEIVED, 0xAA)) == 2
    assert len(encode_symbol(RECEIVED, 0x37)) == 1


def test_short_form_byte_after_an_orphan_first_half_is_kept():
    # a desynced first half followed by short-form bus bytes: ebusd keeps them
    events = EnhDecoder().feed(bytes((0xC4, 0x45, 0x31, 0x32)))
    assert events == [(RECEIVED, 0x45), (RECEIVED, 0x31), (RECEIVED, 0x32)]


def test_host_commands_always_use_the_two_byte_form():
    from aioebus.transport import SEND, START, encode_command
    assert encode_command(SEND, 0x10) == bytes((0xC4, 0x90))  # never short, unlike RECEIVED
    assert encode_symbol(RECEIVED, 0x10) == b"\x10"
    assert EnhDecoder().feed(encode_command(START, 0x31)) == [(START, 0x31)]
