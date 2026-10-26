"""Device scan (eBUS identification, command 0704).

A master reads 0704 from a slave address; the slave answers with 10 bytes:
manufacturer (1), device id (5 ASCII, null-padded), software version (2 BCD) and
hardware version (2 BCD). ebusd formats SW/HW as four digits (e.g. 0902, 5103)
and derives the numbers used by `id_sw`/`id_hw` conditions from them.

Validated against real 0704 answers captured from the reference bus:
    b5 48 4d55303030 0902 5103 -> Vaillant HMU00 sw=0902 hw=5103
    b5 43 54 4c5633  0808 8004 -> Vaillant CTLV3 sw=0808 hw=8004
"""
from __future__ import annotations

from dataclasses import dataclass

SCAN_PBSB = "0704"

# a few known manufacturer bytes (eBUS); extend as needed
_MANUFACTURERS = {0xB5: "Vaillant", 0xB7: "Kromschroeder", 0x06: "Dungs"}


@dataclass
class ScanResult:
    mf: int               # raw manufacturer byte
    manufacturer: str     # resolved name, or hex
    ident: str            # 5-char device id (e.g. "HMU00", "CTLV3")
    sw: str               # software version as 4 digits (e.g. "0902")
    hw: str               # hardware version as 4 digits (e.g. "5103")
    sw_num: int | None    # id_sw for conditions, or None if not decimal
    hw_num: int | None    # id_hw for conditions


def _four(b0: int, b1: int) -> str:
    return f"{b0:02x}{b1:02x}"


def _num(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def decode_scan(slave: bytes) -> ScanResult:
    """Decode the 10-byte 0704 identification answer."""
    if len(slave) < 10:
        raise ValueError(f"scan answer too short: {slave.hex()}")
    mf = slave[0]
    ident = slave[1:6].rstrip(b"\x00").decode("latin-1").rstrip()
    sw = _four(slave[6], slave[7])
    hw = _four(slave[8], slave[9])
    return ScanResult(mf, _MANUFACTURERS.get(mf, f"{mf:#04x}"), ident, sw, hw,
                      _num(sw), _num(hw))


@dataclass
class Identification:
    """Vaillant extended identification (read from b509 24/25/26). The 0704 scan
    only gives the control-board id (e.g. HMU00), which is shared across many
    appliances; this adds the 10-digit article number (Sachnummer) that maps to
    the actual model, the full factory serial, and the manufacture year/week."""
    article: str  # 10-digit article number (Sachnummer)
    serial: str   # full factory serial (unique per unit)
    year: int | None
    week: int | None


def _digits(raw: bytes) -> str:
    """ASCII digits from a raw answer, dropping status/padding bytes."""
    return "".join(chr(b) for b in raw if 0x30 <= b <= 0x39)


def _suffix(raw: bytes) -> str:
    """The revision tail of field 27 -- a letter and one digit (e.g. 'N3'). The
    rest is padding (which varies: '<', '0' or null), so take just the two chars
    the Vaillant app shows."""
    return raw[:2].decode("latin-1", "ignore")


def decode_identification(
    f24: bytes, f25: bytes, f26: bytes, f27: bytes
) -> Identification | None:
    """Decode the Vaillant identification from the four b509 reads (24/25/26/27).

    The article number is the last two digits of field 24 plus the first eight of
    field 25 -- validated against a sensoCOMFORT VRC 720, whose manual lists
    0010045478. The serial is the full 28-character string exactly as the Vaillant
    app shows it (24 + 25 + 26 + the 'N#' of 27, e.g. 21233400100211220001015434N3).
    Field 24 is '21' + year + week + the first two article digits. Returns None
    when the bytes are not a Vaillant identification."""
    d24, d25, d26 = _digits(f24), _digits(f25), _digits(f26)
    if len(d24) < 8 or not d24.startswith("21") or len(d25) < 8:
        return None
    return Identification(
        article=d24[6:8] + d25[:8],
        serial=d24 + d25 + d26 + _suffix(f27),
        year=2000 + int(d24[2:4]) if d24[2:4].isdigit() else None,
        week=int(d24[4:6]) if d24[4:6].isdigit() else None,
    )
