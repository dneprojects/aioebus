"""eBUS datatype codecs -- Python port of ebusd's datatype.cpp.

Each codec turns the raw field bytes (as on the bus, little-endian for multi-byte
numbers) into a Python value, honouring ebusd's "replacement"/invalid sentinels
-> None, and encodes numeric values for writes. Pure, offline, testable against
real captures. No bus, no I/O.
"""
from __future__ import annotations

import math
import struct
from typing import Literal
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# base numeric types: name -> (size_bytes, signed, base_divisor, replacement_raw)
# replacement_raw None means "no replacement" (whole range valid, e.g. U2L).
_NUMERIC = {
    "UCH": (1, False, 1, 0xFF),
    "SCH": (1, True, 1, 0x80),
    "UIN": (2, False, 1, 0xFFFF),
    "SIN": (2, True, 1, 0x8000),
    "ULG": (4, False, 1, 0xFFFFFFFF),
    "U2L": (2, False, 1, None),
    "U4L": (4, False, 1, None),
    "D1B": (1, True, 1, 0x80),
    "D1C": (1, False, 2, 0xFF),
    "D2B": (2, True, 256, 0x8000),
    "D2C": (2, True, 16, 0x8000),
    "FLT": (2, True, 1000, 0x8000),
}

# types whose valid raw range ends below the byte's maximum (ebusd: D1C 0..100.0)
_MAX_RAW = {"D1C": 0xC8}

# reversed (high-byte-first) integer variants: name -> same tuple, big-endian
_NUMERIC_REV = {
    "UIR": (2, False, 1, 0xFFFF),
    "SIR": (2, True, 1, 0x8000),
}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _bcd(b: int) -> int | None:
    hi, lo = b >> 4, b & 0x0F
    return None if hi > 9 or lo > 9 else hi * 10 + lo


@dataclass
class Codec:
    name: str
    length: int  # -1 = remainder (variable length)

    def decode(self, data: bytes):  # pragma: no cover
        raise NotImplementedError


class _Numeric(Codec):
    def __init__(self, name, size, signed, base_div, repl, big=False):
        super().__init__(name, size)
        self.signed = signed
        self.base_div = base_div
        self.repl = repl
        self.big = big  # byte order; True for the reversed types (UIR/SIR)
        self.extra_div: float | None = None  # from the CSV divisor column
        self.max_raw = _MAX_RAW.get(name)  # a type narrower than its bytes (D1C)

    def decode(self, data: bytes):
        if len(data) < self.length:
            return None
        raw = int.from_bytes(data[: self.length], "big" if self.big else "little")
        if raw == self.repl or (self.max_raw is not None and raw > self.max_raw):
            return None
        if self.signed and raw >= 1 << (8 * self.length - 1):
            raw -= 1 << (8 * self.length)
        value: float = raw
        if self.base_div != 1:
            value = value / self.base_div
        if self.extra_div:  # ebusd: positive divides, negative multiplies
            value = value / self.extra_div if self.extra_div > 0 else value * -self.extra_div
        return value

    def encode(self, value) -> bytes:
        """Inverse of decode: a Python value -> the raw little/big-endian bytes.
        None encodes the replacement (invalid) sentinel."""
        order: Literal["big", "little"] = "big" if self.big else "little"
        if value is None:
            if self.repl is None:
                raise ValueError(f"{self.name}: no replacement value to encode None")
            return self.repl.to_bytes(self.length, order)
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{self.name}: value {value} is not a finite number")
        if self.extra_div:  # inverse of decode's divide/multiply
            v = v * self.extra_div if self.extra_div > 0 else v / -self.extra_div
        if self.base_div != 1:
            v = v * self.base_div
        # half away from zero, like ebusd (round() would send 40.5 as 40, not 41)
        raw = math.floor(abs(v) + 0.5) * (1 if v >= 0 else -1)
        bits = 8 * self.length
        lo, hi = (-(1 << (bits - 1)), 1 << (bits - 1)) if self.signed else (0, 1 << bits)
        if not lo <= raw < hi or (self.max_raw is not None and raw > self.max_raw):
            # checked before two's complement, so nothing wraps
            raise ValueError(f"{self.name}: value {value} out of range")
        if self.signed and raw < 0:
            raw += 1 << bits
        if raw == self.repl:  # the device would read "no value", not this number
            raise ValueError(f"{self.name}: value {value} is the replacement (invalid) value")
        return raw.to_bytes(self.length, order)


class _Bcd(Codec):
    def __init__(self, n=1):
        super().__init__("BCD" if n == 1 else f"BCD:{n}", n)

    def decode(self, data: bytes):
        if len(data) < self.length:
            return None
        value = 0
        for b in reversed(data[: self.length]):  # little-endian
            hi, lo = b >> 4, b & 0x0F
            if b == 0xFF or hi > 9 or lo > 9:
                return None
            value = value * 100 + hi * 10 + lo
        return value


class _Ign(Codec):
    """Consumes `n` bytes (n < 0: the rest) and yields no value (ebusd IGN:n)."""

    def __init__(self, n=1):
        super().__init__("IGN:*" if n < 0 else f"IGN:{n}", n)

    def decode(self, data: bytes):
        return None


class _Hex(Codec):
    def __init__(self, n=1):  # n < 0 -> remainder
        super().__init__("HEX:*" if n < 0 else f"HEX:{n}", n)

    def decode(self, data: bytes):
        if self.length >= 0 and len(data) < self.length:
            return None
        chunk = data if self.length < 0 else data[: self.length]
        return " ".join(f"{b:02x}" for b in chunk)


class _Str(Codec):
    def __init__(self, n):  # n < 0 -> remainder
        super().__init__("STR:*" if n < 0 else f"STR:{n}", n)

    def decode(self, data: bytes):
        if self.length >= 0 and len(data) < self.length:
            return None
        chunk = data if self.length < 0 else data[: self.length]
        # as ebusd: the text ends at the first NUL, a control character reads as
        # a space and another unprintable one as '?' (Latin-1 letters are kept)
        text = chunk.split(b"\x00", 1)[0].decode("latin-1")
        return "".join(" " if c < " " else "?" if "\x7f" <= c < "\xa0" else c
                       for c in text).rstrip()


class _TruncTime(Codec):
    """1-byte truncated time: value counts `step` minutes from 00:00 (TTM/10,
    TTH/30, TTQ/15). The replacement raw reads as no value."""

    def __init__(self, name, step, repl, bits=8):
        super().__init__(name, 1)
        self.step = step
        self.repl = repl
        self.mask = (1 << bits) - 1  # TTH/TTQ use the low 6/7 bits only (ebusd)

    def decode(self, data: bytes):
        if len(data) < 1 or (raw := data[0] & self.mask) == self.repl:
            return None
        mins = raw * self.step
        if mins > 1440:
            return None
        return f"{mins // 60:02d}:{mins % 60:02d}"


class _ClockTime(Codec):
    """hh:mm or hh:mm:ss. `bcd` picks BCD vs plain-int bytes; `reversed_` means
    the bytes run smallest unit first (minute[,second] before hour)."""

    def __init__(self, name, nbytes, *, bcd, reversed_):
        super().__init__(name, nbytes)
        self.bcd = bcd
        self.reversed_ = reversed_
        self.parts = nbytes  # 2 -> h,m ; 3 -> h,m,s

    def decode(self, data: bytes):
        if len(data) < self.length or any(b == 0xFF for b in data[: self.length]):
            return None
        raw = list(data[: self.length])
        comps = raw[::-1] if self.reversed_ else raw  # -> hour, minute[, second]
        vals = [v for v in (_bcd(b) if self.bcd else b for b in comps) if v is not None]
        if len(vals) != len(comps):
            return None
        h, m = vals[0], vals[1]
        s = vals[2] if self.parts == 3 else 0
        # 24:00 is valid (end of a day, e.g. a timer slot until midnight), as in ebusd
        if h > 24 or m > 59 or s > 59 or (h == 24 and (m or s)):
            return None
        return f"{h:02d}:{m:02d}:{s:02d}" if self.parts == 3 else f"{h:02d}:{m:02d}"


class _Date(Codec):
    """dd.mm.yyyy. Bytes are day, month, [weekday (4-byte form only)], year-2000,
    as BCD (BDA) or plain int (HDA)."""

    def __init__(self, name, nbytes, *, bcd):
        super().__init__(name, nbytes)
        self.bcd = bcd

    def decode(self, data: bytes):
        if len(data) < self.length or any(b == 0xFF for b in data[: self.length]):
            return None
        raw = list(data[: self.length])
        conv = (lambda b: _bcd(b)) if self.bcd else (lambda b: b)
        day, month = conv(raw[0]), conv(raw[1])
        year = conv(raw[3]) if self.length == 4 else conv(raw[2])  # skip weekday
        if day is None or month is None or year is None:
            return None
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return None
        return f"{day:02d}.{month:02d}.{2000 + year:04d}"


class _Weekday(Codec):
    def __init__(self, name, first, repl):
        super().__init__(name, 1)
        self.first = first  # raw value that means Monday
        self.repl = repl

    def decode(self, data: bytes):
        if len(data) < 1 or data[0] == self.repl:
            return None
        idx = data[0] - self.first
        return _WEEKDAYS[idx] if 0 <= idx < 7 else None


class _DateTime(Codec):
    """DTM: 4-byte little-endian minute count since 2009-01-01 00:00."""

    _EPOCH = datetime(2009, 1, 1)

    def __init__(self):
        super().__init__("DTM", 4)

    def decode(self, data: bytes):
        if len(data) < 4:
            return None
        raw = int.from_bytes(data[:4], "little")
        if raw == 0xFFFFFFFF:
            return None
        try:
            when = self._EPOCH + timedelta(minutes=raw)
        except OverflowError:  # beyond year 9999: no real time
            return None
        return when.strftime("%d.%m.%Y %H:%M")  # date as DAY/HDA, like ebusd


class _Days(Codec):
    """DAY: 2-byte little-endian day count since 1900-01-01."""

    _EPOCH = date(1900, 1, 1)

    def __init__(self):
        super().__init__("DAY", 2)

    def decode(self, data: bytes):
        if len(data) < 2:
            return None
        raw = int.from_bytes(data[:2], "little")
        if raw == 0xFFFF:
            return None
        return (self._EPOCH + timedelta(days=raw)).strftime("%d.%m.%Y")


class _Pin(Codec):
    """PIN: 2-byte BCD, high byte first, four decimal digits."""

    def __init__(self):
        super().__init__("PIN", 2)

    def decode(self, data: bytes):
        if len(data) < 2 or data[:2] == b"\xff\xff":
            return None
        hi, lo = _bcd(data[0]), _bcd(data[1])
        return None if hi is None or lo is None else f"{hi:02d}{lo:02d}"


class _Bit(Codec):
    """A bit field inside a shared byte: BIx reads `nbits` bits starting at bit x.
    Its byte length is 0 -- the message decoder groups consecutive bit fields into
    one byte and advances past it when the group ends (see messages.decode)."""

    def __init__(self, name, start, nbits):
        super().__init__(name, 0)
        self.start = start
        self.nbits = nbits

    def decode_bits(self, byte: int):
        return (byte >> self.start) & ((1 << self.nbits) - 1)

    def decode(self, data: bytes):  # standalone use: take bits from byte 0
        return None if not data else self.decode_bits(data[0])


class _Float(Codec):
    """EXP/EXR: IEEE-754 binary32, little- or big-endian."""

    def __init__(self, name, big):
        super().__init__(name, 4)
        self.fmt = ">f" if big else "<f"
        self.extra_div: float | None = None  # from the CSV divisor column

    def decode(self, data: bytes):
        if len(data) < 4:
            return None
        value = struct.unpack(self.fmt, data[:4])[0]
        if value != value:  # NaN (replacement) -> None
            return None
        if self.extra_div:  # ebusd: positive divides, negative multiplies
            value = value / self.extra_div if self.extra_div > 0 else value * -self.extra_div
        return value

    def encode(self, value) -> bytes:
        """Inverse of decode; None encodes the replacement (NaN)."""
        if value is None:
            return struct.pack(self.fmt, float("nan"))
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{self.name}: value {value} is not a finite number")
        if self.extra_div:  # inverse of decode's divide/multiply
            v = v * self.extra_div if self.extra_div > 0 else v / -self.extra_div
        try:
            return struct.pack(self.fmt, v)
        except OverflowError as err:
            raise ValueError(f"{self.name}: value {value} out of range") from err


# base name -> factory(n_or_None). n is the suffix (bytes) where adjustable.
_SPECIAL = {
    "TTM": lambda n: _TruncTime("TTM", 10, 0x90),
    "TTH": lambda n: _TruncTime("TTH", 30, 0x00, bits=6),
    "TTQ": lambda n: _TruncTime("TTQ", 15, 0x00, bits=7),
    "HTM": lambda n: _ClockTime("HTM", 2, bcd=False, reversed_=False),
    "BTM": lambda n: _ClockTime("BTM", 2, bcd=True, reversed_=True),
    "VTM": lambda n: _ClockTime("VTM", 2, bcd=False, reversed_=True),
    "HTI": lambda n: _ClockTime("HTI", 3, bcd=False, reversed_=False),
    "BTI": lambda n: _ClockTime("BTI", 3, bcd=True, reversed_=True),
    "VTI": lambda n: _ClockTime("VTI", 3, bcd=False, reversed_=True),
    "HDA": lambda n: _Date("HDA", n or 4, bcd=False),
    "BDA": lambda n: _Date("BDA", n or 4, bcd=True),
    "BDY": lambda n: _Weekday("BDY", 0, 0x07),
    "HDY": lambda n: _Weekday("HDY", 1, 0x00),
    "DTM": lambda n: _DateTime(),
    "DAY": lambda n: _Days(),
    "PIN": lambda n: _Pin(),
    "EXP": lambda n: _Float("EXP", big=False),
    "EXR": lambda n: _Float("EXR", big=True),
}


def make_codec(spec: str) -> Codec:
    """Build a codec from a type spec like 'D2C', 'UIN', 'HEX:6', 'STR:*'."""
    spec = spec.strip().upper()
    base, _, suffix = spec.partition(":")
    n = -1 if suffix == "*" else (int(suffix) if suffix else None)
    if base == "IGN":
        return _Ign(n if n and (n > 0 or n == -1) else 1)
    if base == "HEX":
        return _Hex(n if n and (n > 0 or n == -1) else 1)
    if base == "STR":
        return _Str(n if n is not None else 1)
    if base == "BCD":
        return _Bcd(n if n and n > 0 else 1)
    if len(base) == 3 and base[:2] == "BI" and base[2].isdigit():
        return _Bit(base, int(base[2]), n if n and n > 0 else 1)
    if base in _SPECIAL:
        return _SPECIAL[base](n if n and n > 0 else None)
    if base in _NUMERIC:
        size, signed, bdiv, repl = _NUMERIC[base]
        return _Numeric(base, size, signed, bdiv, repl)
    if base in _NUMERIC_REV:
        size, signed, bdiv, repl = _NUMERIC_REV[base]
        return _Numeric(base, size, signed, bdiv, repl, big=True)
    raise ValueError(f"unknown eBUS type: {spec!r}")


def decode(spec: str, data, divisor: float | None = None):
    """Decode a field. `data` may be bytes or a hex string. `divisor` is the
    CSV divisor column (applied on top of the base type's own divisor)."""
    if isinstance(data, str):
        data = bytes.fromhex(data)
    codec = make_codec(spec)
    if divisor and hasattr(codec, "extra_div"):  # _Numeric and _Float (EXP/EXR)
        codec.extra_div = divisor
    return codec.decode(data)


def is_numeric(spec: str) -> bool:
    """True for an integer type (with or without a base divisor) whose value range
    is known, i.e. one that can back a number control."""
    try:
        return isinstance(make_codec(spec), _Numeric)
    except ValueError:
        return False


def value_range(spec: str, divisor: float | None = None) -> tuple[float, float, float] | None:
    """(min, max, step) of the values an integer type can carry -- its replacement
    (no value) excluded, the divisor column applied (negative multiplies) -- or
    None for any other type."""
    try:
        codec = make_codec(spec)
    except ValueError:
        return None
    if not isinstance(codec, _Numeric):
        return None
    bits = 8 * codec.length
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if codec.signed else (0, (1 << bits) - 1)
    if codec.repl is not None:
        repl = codec.repl - (1 << bits) if codec.signed and codec.repl >= 1 << (bits - 1) else codec.repl
        lo, hi = (lo + 1 if repl == lo else lo), (hi - 1 if repl == hi else hi)
    if codec.max_raw is not None:
        hi = min(hi, codec.max_raw)
    scale = codec.base_div * (divisor if divisor and divisor > 0 else 1)
    factor = -divisor if divisor and divisor < 0 else 1
    # divide by the scale (not multiply by its inverse): 65534 / 10 is 6553.4 exactly
    return lo * factor / scale, hi * factor / scale, factor / scale


def encode(spec: str, value, divisor: float | None = None) -> bytes:
    """Encode a value to raw field bytes (numeric and float types, for writes)."""
    codec = make_codec(spec)
    if not isinstance(codec, (_Numeric, _Float)):
        raise NotImplementedError(f"encode not supported for type {spec!r}")
    if divisor:
        codec.extra_div = divisor
    return codec.encode(value)
