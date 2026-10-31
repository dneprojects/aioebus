"""Structured message definitions + the ebusd-CSV ingest path.

The in-memory model is structured dataclasses -- never CSV-shaped. The community
ebusd-CSV library is read *into* this model via `parse_csv` (using Python's csv
module, so quoted commas no longer abort a file the way they did in ebusd). Our
own curated definitions can instead come from the native JSON format in
`convert.py`; both end up as the same `MessageDef` objects.

Scope (Phase 2 core): the common Vaillant row shape
    type,circuit,level,name,comment,qq,zz,pbsb,id,[ name,part,type,divisor/values,unit,comment ]*
with default rows (*r / *w), IGN fields, value tables and the divisor column.
Conditions/templates/chains are TODO.
"""
from __future__ import annotations

import contextlib
import csv
import io
import re
from dataclasses import dataclass

from .codecs import _Bit, make_codec
from .codecs import encode as _encode_field

# Valid message directions (ebusd): read, polled read r1..r9, write, update(+write).
# Anything else in column 0 -- the CSV header line, condition/template rows we do
# not yet support -- is skipped rather than turned into a bogus message.
_DIRECTION = re.compile(r"^(r[1-9]?|w|uw?)$")


@dataclass
class FieldDef:
    name: str
    part: str  # "m" master, "s" slave, "" = default for the message direction
    type: str  # "D2C", "UIN", "HEX:6", "IGN:6", "STR:*", ...
    divisor: float | None
    values: dict[int, str] | None
    unit: str
    comment: str
    # display-text translations by language code ({"en": ..., "de": ...}); the
    # technical `name` stays the language-neutral key, so entity ids are stable
    tr: dict[str, str] | None = None


@dataclass
class MessageDef:
    direction: str  # r, r1..r9, w, u, uw
    circuit: str
    level: str
    name: str
    comment: str
    qq: str
    zz: str
    pbsb: str  # e.g. "b509"
    msgid: str  # hex after pbsb, e.g. "0d1800"
    fields: list[FieldDef]
    conditions: list[str] | None = None  # e.g. ["id_sw>=200"]; evaluated in P4
    # display-text translations by language code for the message-level name
    tr: dict[str, str] | None = None

    @property
    def is_read(self) -> bool:
        return self.direction[:1] in "ru"

    @property
    def is_write(self) -> bool:
        return "w" in self.direction

    @property
    def poll_priority(self) -> int:
        tail = self.direction[1:]
        return int(tail) if self.direction[:1] == "r" and tail.isdigit() else 0

    def decode(self, master: bytes | str = b"", slave: bytes | str = b"") -> dict:
        """Decode a telegram's data into {field_name: value}."""
        if isinstance(master, str):
            master = bytes.fromhex(master)
        if isinstance(slave, str):
            slave = bytes.fromhex(slave)
        bufs = {"m": master, "s": slave}
        pos = {"m": 0, "s": 0}
        # Only a pure read puts its payload in the slave answer; writes, updates
        # (u) and uw commands carry their fields in the master part.
        default_part = "s" if self.direction[:1] == "r" else "m"
        out: dict = {}
        bit_part: str | None = None  # part whose byte an open bit group sits in
        last_start = -1

        def close_bits():
            nonlocal bit_part, last_start
            if bit_part is not None:
                pos[bit_part] += 1  # consume the shared bit byte
                bit_part, last_start = None, -1

        for f in self.fields:
            part = f.part if f.part in ("m", "s") else default_part
            codec = make_codec(f.type)

            if isinstance(codec, _Bit):
                # new byte if no group is open, the part changed, or the bit
                # position did not advance (a fresh flag byte starts)
                if bit_part != part or codec.start <= last_start:
                    close_bits()
                    bit_part = part
                buf, p = bufs[part], pos[part]
                val = codec.decode_bits(buf[p]) if p < len(buf) else None
                last_start = codec.start
                if f.values and isinstance(val, int) and val in f.values:
                    val = f.values[val]
                if f.name:
                    out[f.name] = val
                continue

            close_bits()  # a non-bit field ends any open bit group
            if f.divisor is not None and hasattr(codec, "extra_div"):
                codec.extra_div = f.divisor
            buf, p = bufs[part], pos[part]
            if codec.length < 0:  # remainder
                chunk, pos[part] = buf[p:], len(buf)
            else:
                chunk, pos[part] = buf[p : p + codec.length], p + codec.length
            if f.type.upper().startswith("IGN"):
                continue
            val = codec.decode(chunk)
            if f.values and isinstance(val, (int, float)) and int(val) in f.values:
                val = f.values[int(val)]
            if f.name:
                out[f.name] = val
        close_bits()
        return out

    def encode(self, values: dict) -> bytes:
        """Build the master data (message id + encoded master fields) for a write.

        `values` is keyed by field name; a message with a single value field also
        accepts the message name (as its decoded label is). Enum labels are mapped
        back to their numbers. Numeric and float types only -- bit-field and
        string writes are not supported yet and raise."""
        data = bytearray(bytes.fromhex(self.msgid))
        named = [f for f in self.fields
                 if f.name and not f.type.upper().startswith("IGN")
                 and (f.part if f.part in ("m", "s") else "m") == "m"]
        lone = named[0].name if len(named) == 1 else None
        for f in self.fields:
            part = f.part if f.part in ("m", "s") else "m"  # writes carry fields in master
            if part != "m":
                continue
            if f.type.upper().startswith("IGN"):
                n = int(f.type.split(":")[1]) if ":" in f.type else 1
                data += bytes(n)
                continue
            if f.name in values:
                val = values[f.name]
            elif f.name in (lone, "value") and self.name in values:
                val = values[self.name]
            else:
                raise ValueError(f"{self.name}: missing value for field {f.name!r}")
            if val is None:  # would send the type's "no value" marker to the device
                raise ValueError(f"{self.name}: no value given for field {f.name!r}")
            if f.values and isinstance(val, str):  # map an enum label back to its number
                reverse = {label: num for num, label in f.values.items()}
                if val not in reverse:
                    raise ValueError(f"{self.name}: {val!r} is not a valid option for {f.name}")
                val = reverse[val]
            data += _encode_field(f.type, val, f.divisor)
        return bytes(data)

    def decode_labeled(self, master: bytes | str = b"", slave: bytes | str = b"") -> dict:
        """Decode into HA-style labels: a lone generic ``value`` field becomes the
        message name; any other field is prefixed with it (as HA entities are named,
        e.g. ``Currenterror error`` or ``SensorData1 s1``)."""
        raw = self.decode(master, slave)
        if list(raw) == ["value"]:
            return {self.name: raw["value"]}
        return {(self.name if k == "value" else f"{self.name} {k}"): v for k, v in raw.items()}


def _split_divisor_values(text: str):
    text = text.strip()
    if not text:
        return None, None
    if "=" in text or ";" in text:  # value table like 0=off;1=on
        vals: dict[int, str] = {}
        for part in text.split(";"):
            key, sep, label = part.partition("=")
            if sep:
                with contextlib.suppress(ValueError):
                    vals[int(key)] = label
        return None, (vals or None)
    try:  # divisor (positive divides, negative multiplies)
        return (float(text) if "." in text else int(text)), None
    except ValueError:
        return None, None


def parse_csv(text: str) -> list[MessageDef]:
    defaults: dict[str, list[str]] = {}
    out: list[MessageDef] = []
    for row in csv.reader(io.StringIO(text)):  # csv handles quoted commas
        if not row:
            continue
        t = (row[0] or "").strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("*"):  # default row, e.g. *r,elo
            defaults[t[1:2]] = row
            continue
        # strip any leading condition prefixes ([id_sw>=200], possibly stacked);
        # they are kept as metadata and evaluated against scan data in Phase 4.
        conditions = re.findall(r"\[([^]]*)\]", t) if t.startswith("[") else []
        core = re.sub(r"^(?:\[[^]]*\])+", "", t)
        if not _DIRECTION.match(core):  # header, !load/!include instruction, etc.
            continue

        def c(i, _row=row):
            return _row[i].strip() if i < len(_row) else ""

        d = defaults.get(core[:1], [])

        def g(i, _d=d):  # fixed column with default fallback
            return c(i) or (_d[i].strip() if i < len(_d) else "")

        fields: list[FieldDef] = []
        i = 9
        while i < len(row):
            name, part, typ = c(i), c(i + 1), c(i + 2)
            div, vals = _split_divisor_values(c(i + 3))
            unit, cmt = c(i + 4), c(i + 5)
            i += 6
            if typ:
                fields.append(FieldDef(name, part, typ, div, vals, unit, cmt))
        out.append(
            MessageDef(
                direction=core, circuit=g(1), level=g(2), name=c(3), comment=c(4),
                qq=g(5), zz=g(6), pbsb=g(7), msgid=g(8), fields=fields,
                conditions=conditions or None,
            )
        )
    return out
