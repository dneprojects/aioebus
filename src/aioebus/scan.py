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


def _ascii(raw: bytes) -> str:
    """Printable ASCII payload of a field: keeps digits, letters and the '<'
    padding, dropping the leading status byte and any control/pad nulls."""
    return "".join(chr(b) for b in raw if 0x21 <= b <= 0x7E)


def decode_identification(
    f24: bytes, f25: bytes, f26: bytes, f27: bytes
) -> Identification | None:
    """Decode the Vaillant identification from the four b509 reads (24/25/26/27).

    The four fields concatenate to the 28-character serial exactly as the Vaillant
    app shows it. Its documented layout is '21' (data identifier) + year + week +
    10-char article number (a 6-digit one is padded with '<') + factory mark +
    product counter + 'N' + check digit. The article number maps to the actual
    model and is validated against a sensoCOMFORT VRC 720 (0010045478). Returns
    None when the bytes are not a Vaillant identification."""
    full = _ascii(f24) + _ascii(f25) + _ascii(f26) + _ascii(f27)[:2]
    if len(full) < 16 or full[:2] != "21" or not full[2:6].isdigit():
        return None
    return Identification(
        article=full[6:16].rstrip("<"),  # 6-digit articles are '<'-padded to 10
        serial=full,
        year=2000 + int(full[2:4]),
        week=int(full[4:6]),
    )
