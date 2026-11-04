"""Structured message definitions + the ebusd-CSV ingest path.

The in-memory model is structured dataclasses -- never CSV-shaped. The community
ebusd-CSV library is read *into* this model via `parse_csv` (using Python's csv
module, so quoted commas no longer abort a file the way they did in ebusd). Our
own curated definitions can instead come from the native JSON format in
`convert.py`; both end up as the same `MessageDef` objects.

Supported: the common Vaillant row shape
    type,circuit,level,name,comment,qq,zz,pbsb,id,[ name,part,type,divisor/values,unit,comment ]*
with default rows (as ebusd applies them), IGN fields, value tables, constant
values (``=x``), the divisor column and condition prefixes (evaluated by
conditions.py); templates and chained ids are not.
"""
from __future__ import annotations

import contextlib
import csv
import io
import logging
import re
from dataclasses import dataclass, replace
from functools import cached_property

from .codecs import BitCodec, NumericCodec, make_codec
from .codecs import decode as _decode_field
from .codecs import encode as _encode_field
from .frame import BROADCAST, is_master

# Valid message directions (ebusd): read, polled read r1..r9, write, update(+write).
# Anything else in column 0 -- the CSV header line, condition/template rows we do
# not yet support -- is skipped rather than turned into a bogus message.
_DIRECTION = re.compile(r"^(r[1-9]?|w|uw?)$")

_LOGGER = logging.getLogger(__name__)


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
    # a constant (``=x`` in the values column): always written, never a value;
    # ``verify`` (``==x``) also requires it in a decoded telegram
    const: str | None = None
    verify: bool = False

    @property
    def ignored(self) -> bool:
        return self.type.upper().startswith("IGN")

    @property
    def has_value(self) -> bool:
        """Named, neither ignored nor a constant: the field yields a value."""
        return bool(self.name) and not self.ignored and self.const is None


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
    conditions: list[str] | None = None  # e.g. ["id_sw>=200"] (see conditions.py)
    # display-text translations by language code for the message-level name
    tr: dict[str, str] | None = None

    @cached_property
    def id_bytes(self) -> bytes:
        """The message id (the leading master data after PBSB) as bytes."""
        return bytes.fromhex(self.msgid)

    @property
    def address(self) -> int | None:
        """The destination address (ZZ), None when the message names none."""
        if len(self.zz) == 2 and _is_hex(self.zz):
            return int(self.zz, 16)
        return None

    @property
    def value_fields(self) -> list[FieldDef]:
        """The fields that yield a value (named, not IGN, no constant): one entity each."""
        return [f for f in self.fields if f.has_value]

    @property
    def is_polled_read(self) -> bool:
        """A true read (r, r1..r9), not a passive update or a write."""
        return self.direction[:1] == "r"

    @property
    def is_read(self) -> bool:
        return self.direction[:1] in "ru"

    @property
    def is_write(self) -> bool:
        return "w" in self.direction

    @property
    def is_passive(self) -> bool:
        """A telegram other masters send (u, uw): decoded, never sent by us."""
        return self.direction[:1] == "u"

    @property
    def poll_priority(self) -> int:
        tail = self.direction[1:]
        return int(tail) if self.direction[:1] == "r" and tail.isdigit() else 0

    def part_of(self, f: FieldDef) -> str:
        """Where a field lives, as in ebusd: everything in the master data when
        the destination is a broadcast or a master (nobody answers), else its own
        part, else the master data for a write (w, uw) and the slave's answer for
        a read (r, u)."""
        zz = self.address
        if zz is not None and (zz == BROADCAST or is_master(zz)):
            return "m"
        part = f.part.lower()
        if part in ("m", "s"):
            return part
        return "m" if self.is_write else "s"

    def request_fields(self) -> list[FieldDef]:
        """The value fields sent in the request (a write's value, a read's arguments)."""
        return [f for f in self.value_fields if self.part_of(f) == "m"]

    @property
    def needs_args(self) -> bool:
        """A read whose request carries values (named master fields, e.g. an
        index): it cannot be polled without them."""
        return self.is_polled_read and bool(self.request_fields())

    def decode(self, master: bytes | str = b"", slave: bytes | str = b"") -> dict:
        """Decode a telegram's data into {field_name: value}."""
        if isinstance(master, str):
            master = bytes.fromhex(master)
        if isinstance(slave, str):
            slave = bytes.fromhex(slave)
        bufs = {"m": master, "s": slave}
        pos = {"m": 0, "s": 0}
        out: dict = {}
        bit_part: str | None = None  # part whose byte an open bit group sits in
        last_start = -1

        def close_bits():
            nonlocal bit_part, last_start
            if bit_part is not None:
                pos[bit_part] += 1  # consume the shared bit byte
                bit_part, last_start = None, -1

        for f in self.fields:
            part = self.part_of(f)
            codec = make_codec(f.type, f.divisor)

            if isinstance(codec, BitCodec):
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
            buf, p = bufs[part], pos[part]
            if codec.length < 0:  # remainder
                chunk, pos[part] = buf[p:], len(buf)
            else:
                chunk, pos[part] = buf[p : p + codec.length], p + codec.length
            if f.ignored:
                continue
            if f.const is not None:
                if f.verify and not _is_const(f, chunk):
                    raise ValueError(f"{self.name}: {f.name or 'field'} is not {f.const}")
                continue
            if f.values and isinstance(codec, NumericCodec):
                # as ebusd: the table is looked up by the raw value first, then
                # the replacement means no value, else the raw number shows
                raw = codec.raw(chunk)
                if raw is None:
                    val = None
                elif raw == codec.repl:
                    val = f.values.get(raw)
                else:
                    val = f.values.get(raw, raw)
            else:
                val = codec.decode(chunk)
                if f.values and isinstance(val, int) and val in f.values:
                    val = f.values[val]
            if f.name:
                out[f.name] = val
        close_bits()
        return out

    def encode(self, values: dict) -> bytes:
        """Build the master data (message id + encoded master fields): a write's
        payload, or the arguments of a read that takes some (``needs_args``).

        `values` is keyed by field name; a message with a single value field also
        accepts the message name (as its decoded label is). Enum labels are mapped
        back to their numbers. Numeric, float and date types only -- bit-field and
        string writes are not supported yet and raise. A constant field is
        written by itself; a value for a field of the answer is refused (the
        request could not carry it)."""
        data = bytearray(self.id_bytes)
        named = self.request_fields()
        lone = named[0].name if len(named) == 1 else None
        answer_only = {f.name for f in self.value_fields} - {f.name for f in named}
        if stray := answer_only & set(values):
            raise ValueError(f"{self.name}: {sorted(stray)} not part of the request")
        for f in self.fields:
            if self.part_of(f) != "m":
                continue
            if f.ignored:
                data += bytes(max(make_codec(f.type).length, 0))  # IGN:* -> nothing
                continue
            if f.const is not None:
                data += _const_bytes(f)
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
            codec = make_codec(f.type)
            if f.values and isinstance(codec, NumericCodec):  # table values are raw
                data += codec.encode_raw(int(val))
            else:
                data += _encode_field(f.type, val, f.divisor)
        return bytes(data)

    def decode_labeled(self, master: bytes | str = b"", slave: bytes | str = b"") -> dict:
        """Decode into labels (see :func:`label_of`), e.g. ``Currenterror error``
        or ``SensorData1 s1``."""
        return {label_of(self.name, k): v for k, v in self.decode(master, slave).items()}


def label_of(message: str, field: str) -> str:
    """The label a field's value is known by: a generic ``value`` field is the
    message itself, any other is prefixed with the message name."""
    return message if field == "value" else f"{message} {field}"


def _const_bytes(f: FieldDef) -> bytes:
    """The bytes of a constant field (``=x``): hex for HEX, else the type's value."""
    assert f.const is not None
    codec = make_codec(f.type)
    if f.type.upper().startswith("HEX"):
        raw = bytes.fromhex(f.const.replace(" ", ""))
        if codec.length >= 0 and len(raw) != codec.length:
            raise ValueError(f"{f.name}: constant {f.const} does not fit {f.type}")
        return raw
    if isinstance(codec, NumericCodec) and not f.divisor and codec.base_div == 1:
        value = int(f.const)
        if value >= 0:  # e.g. 65535 for U2L: no replacement check
            return codec.encode_raw(value)
    return _encode_field(f.type, float(f.const), f.divisor)


def _is_const(f: FieldDef, chunk: bytes) -> bool:
    try:
        return chunk == _const_bytes(f)
    except (ValueError, NotImplementedError):  # e.g. a time: compare as ebusd shows it
        value = _decode_field(f.type, chunk, f.divisor)
        return value is not None and str(value) == f.const


def _split_divisor_values(text: str):
    """The divisor/values column: (divisor, value table, constant, verify)."""
    text = text.strip()
    if not text:
        return None, None, None, False
    if text.startswith("=") and len(text) > 1:  # constant: =x, or ==x verified
        verify = text.startswith("==")
        return None, None, text[2 if verify else 1:].strip(), verify
    if "=" in text or ";" in text:  # value table like 0=off;1=on
        vals: dict[int, str] = {}
        for part in text.split(";"):
            key, sep, label = part.partition("=")
            if sep:
                with contextlib.suppress(ValueError):
                    vals[int(key)] = label
        return None, (vals or None), None, False
    try:  # divisor (positive divides, negative multiplies)
        return (float(text) if "." in text else int(text)), None, None, False
    except ValueError:
        return None, None, None, False


def usable_fields(name: str, fields: list[FieldDef]) -> list[FieldDef] | None:
    """The fields ready for decoding, or None (logged) when one has a type this
    library cannot decode -- its length is unknown, so no later field could be
    placed either. Repeated field names (upstream CSVs have e.g. temp,temp,temp)
    get ``_1``, ``_2`` suffixes, so every value keeps its own key."""
    for f in fields:
        try:
            make_codec(f.type)
        except ValueError as err:
            _LOGGER.warning("eBUS definition %s skipped: %s", name, err)
            return None
    used = {f.name for f in fields}
    taken: set[str] = set()
    out: list[FieldDef] = []
    for f in fields:
        if f.name and f.name in taken:
            n = 1
            while f"{f.name}_{n}" in taken or f"{f.name}_{n}" in used:
                n += 1  # never collide with another field's own name
            f = replace(f, name=f"{f.name}_{n}")
        taken.add(f.name)
        out.append(f)
    return out


def _row_fields(row: list[str]) -> list[FieldDef]:
    """The field columns of a CSV row (six per field, from column 9)."""
    fields: list[FieldDef] = []
    for i in range(9, len(row), 6):
        name, part, typ, div_values, unit, cmt = (
            (row[j].strip() if j < len(row) else "") for j in range(i, i + 6))
        if typ:
            div, vals, const, verify = _split_divisor_values(div_values)
            fields.append(FieldDef(name, part, typ, div, vals, unit, cmt,
                                   const=const, verify=verify))
    return fields


def _is_hex(text: str) -> bool:
    return len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text)


def _with_default(value: str, default: str, *, star: bool = False,
                  required: bool = False) -> str:
    """ebusd's default rule for a column: an empty value takes the default; with
    ``star`` a '*' in the default is replaced by the value (``required``: an
    empty value stays empty)."""
    if not default or (star and required and not value):
        return value
    if star and "*" in default:
        i = default.index("*")
        return default[:i] + value + default[i + 1:]
    return value or default


def parse_csv(text: str) -> list[MessageDef]:
    """The messages of an ebusd CSV. Default rows (``*r``, ``*w``, ``*u``,
    ``*uw``) apply as in ebusd: per message type (a poll digit ignored), empty
    columns take the default, a message without PBSB gets the default's PBSB
    and ID prefix, and the default's fields come first."""
    defaults: dict[str, tuple[list[str], list[FieldDef]]] = {}
    out: list[MessageDef] = []
    for row in csv.reader(io.StringIO(text)):  # csv handles quoted commas
        if not row:
            continue
        t = (row[0] or "").strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("*"):  # default row, e.g. *r,elo
            defaults[t[1:].lower()] = ([c.strip() for c in row[:9]], _row_fields(row))
            continue
        # leading condition prefixes ([id_sw>=200], possibly stacked) are kept as
        # metadata; the catalog evaluates them for each device
        conditions = re.findall(r"\[([^]]*)\]", t) if t.startswith("[") else []
        core = re.sub(r"^(?:\[[^]]*\])+", "", t)
        if not _DIRECTION.match(core):  # header, !load/!include instruction, etc.
            continue
        cols = [(row[i].strip() if i < len(row) else "") for i in range(9)]
        key = core[:1] if core[:1] == "r" else core  # r1..r9 use the r default
        dcols, dfields = defaults.get(key, ([], []))
        d = dcols + [""] * (9 - len(dcols))
        pbsb, msgid = cols[7], cols[8]
        if not pbsb:  # the default's command, with its ID as the prefix
            pbsb, msgid = d[7], d[8] + msgid
        name = _with_default(cols[3], d[3], star=True, required=True)
        if not name:  # ebusd rejects a nameless message
            continue
        if not _is_hex(pbsb + msgid):  # e.g. a chained id (0d4300:2;0d4308:2): unsupported
            _LOGGER.debug("eBUS definition %s skipped: id %s %s", name, pbsb, msgid)
            continue
        fields = usable_fields(name, [replace(f) for f in dfields] + _row_fields(row))
        if fields is None:
            continue
        out.append(
            MessageDef(
                direction=core,
                circuit=_with_default(cols[1], d[1], star=True),
                level=_with_default(cols[2], d[2], star=True),
                name=name,
                comment=_with_default(cols[4], d[4], star=True),
                qq=_with_default(cols[5], d[5]), zz=_with_default(cols[6], d[6]),
                pbsb=pbsb, msgid=msgid, fields=fields,
                conditions=conditions or None,
            )
        )
    return out
