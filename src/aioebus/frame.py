"""eBUS frame layer -- Python port of ebusd's symbol handling (Phase 3).

Turns the raw bytes on the wire (as captured between two SYN) into a structured
`Telegram`: addresses, the two PBSB command bytes, the master data, and -- for a
master/slave exchange -- the slave's answer. Pure and offline: the escaping, the
eBUS CRC-8 and the frame structure are all validated against real captures in
`tests/test_frame.py` and `tools/validate_frame_db.py`.

The enhanced adapter does bus arbitration and timing in hardware, so the live
transport (Phase 3, transport.py) only has to hand whole frames to this parser.
"""
from __future__ import annotations

from dataclasses import dataclass

SYN = 0xAA  # bus idle / frame separator
ESC = 0xA9  # escape prefix
BROADCAST = 0xFE  # destination address for broadcasts


def crc8(data: bytes) -> int:
    """eBUS CRC-8 (polynomial 0x9B). Confirmed bit-exact against 200/200 real
    master telegrams from the live Vaillant bus."""
    crc = 0
    for byte in data:
        for i in range(8):
            carry = crc & 0x80
            crc = (crc << 1) & 0xFF
            if byte & (0x80 >> i):
                crc |= 1
            if carry:
                crc ^= 0x9B
    return crc


def unescape(raw: bytes) -> bytes:
    """Reverse eBUS escaping: A9 01 -> AA, A9 00 -> A9. SYN never appears inside
    a frame on the wire, so an escape is the only way AA/A9 are carried."""
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if b == ESC and i + 1 < n:
            nxt = raw[i + 1]
            out.append(SYN if nxt == 0x01 else ESC if nxt == 0x00 else nxt)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def escape(data: bytes) -> bytes:
    """Apply eBUS escaping for sending: AA -> A9 01, A9 -> A9 00."""
    out = bytearray()
    for b in data:
        if b == SYN:
            out += bytes((ESC, 0x01))
        elif b == ESC:
            out += bytes((ESC, 0x00))
        else:
            out.append(b)
    return bytes(out)


@dataclass
class Telegram:
    """One decoded eBUS exchange (addresses as raw ints, data as bytes)."""

    qq: int  # source (master) address
    zz: int  # destination address (0xFE = broadcast)
    pb: int  # primary command byte
    sb: int  # secondary command byte
    data: bytes  # master payload (NN bytes)
    crc: int  # master CRC as carried on the wire
    kind: str  # "BC" | "MM" | "MS"
    master_ack: int | None = None  # slave's ACK of the master part (0x00 ok)
    slave_data: bytes = b""  # slave payload (MS only)
    slave_crc: int | None = None
    slave_ack: int | None = None  # master's ACK of the slave part

    @property
    def nn(self) -> int:
        return len(self.data)

    @property
    def pbsb(self) -> str:
        return f"{self.pb:02x}{self.sb:02x}"

    @property
    def master_frame(self) -> bytes:
        """The logical bytes the master CRC is computed over (QQ..last data)."""
        return bytes((self.qq, self.zz, self.pb, self.sb, self.nn)) + self.data

    @property
    def crc_ok(self) -> bool:
        # The eBUS CRC is computed over the escaped (on-wire) byte stream, so a
        # frame carrying an AA/A9 in its data is only verified against escape().
        if crc8(escape(self.master_frame)) != self.crc:
            return False
        if self.kind == "MS" and self.slave_crc is not None:
            slave_frame = bytes((len(self.slave_data),)) + self.slave_data
            return crc8(escape(slave_frame)) == self.slave_crc
        return True


class FrameError(ValueError):
    """Raw bytes did not form a parseable eBUS frame."""


def parse_telegram(raw: bytes | str) -> Telegram:
    """Parse one on-the-wire frame (the bytes between two SYN) into a Telegram.

    `raw` may be bytes or a hex string. Escaping is reversed first, then the
    structure is read by its length fields -- broadcast (ZZ=0xFE) has no answer,
    master/master ends after the ACK, master/slave carries a slave response.
    """
    if isinstance(raw, str):
        raw = bytes.fromhex(raw)
    buf = unescape(raw)
    if len(buf) < 6:
        raise FrameError(f"frame too short: {buf.hex()}")
    # Master part, possibly repeated: if the slave NAKs it (ACK 0xFF) the master
    # resends the whole frame from QQ, so loop until it is ACKed or the capture
    # ends. A garbled first attempt (e.g. a flipped command byte) is discarded.
    pos = 0
    master_ack = None
    while True:
        if pos + 5 > len(buf):
            raise FrameError(f"master header truncated: {buf.hex()}")
        qq, zz, pb, sb, nn = buf[pos:pos + 5]
        end = pos + 5 + nn
        if end + 1 > len(buf):
            raise FrameError(f"master data truncated: {buf.hex()}")
        data = buf[pos + 5:end]
        crc = buf[end]
        pos = end + 1

        if zz == BROADCAST:
            return Telegram(qq, zz, pb, sb, data, crc, "BC")
        if pos >= len(buf):  # master part only, ACK not captured
            return Telegram(qq, zz, pb, sb, data, crc, "MM", master_ack=None)
        master_ack = buf[pos]
        pos += 1
        if master_ack != 0xFF:
            break

    if pos >= len(buf):  # master/master: ACK, no slave payload
        return Telegram(qq, zz, pb, sb, data, crc, "MM", master_ack=master_ack)

    # Slave response, possibly repeated: the master NAKs a bad answer (ACK 0xFF)
    # and the slave resends. Keep reading until an answer is ACKed (0x00) or the
    # buffer ends, so we return the copy ebusd accepted, not a NAKed one.
    slave_data = b""
    slave_crc = slave_ack = None
    while pos < len(buf):
        s_nn = buf[pos]
        pos += 1
        s_end = pos + s_nn
        if s_end >= len(buf):
            raise FrameError(f"slave data truncated: {buf.hex()}")
        slave_data = buf[pos:s_end]
        slave_crc = buf[s_end]
        slave_ack = buf[s_end + 1] if s_end + 1 < len(buf) else None
        pos = s_end + 2
        if slave_ack != 0xFF:  # 0x00 = ACK (accepted), None = end of capture
            break
    return Telegram(
        qq, zz, pb, sb, data, crc, "MS",
        master_ack=master_ack,
        slave_data=slave_data,
        slave_crc=slave_crc,
        slave_ack=slave_ack,
    )
