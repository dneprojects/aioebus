"""What a message's values are: one :class:`Value` per named field, its stable
key, label and cache key, whether it is numeric, and the control a writable one
allows.

Presentation (entity platforms, names, icons) is the consumer's business; this
module only states the facts every consumer derives it from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from .codecs import is_numeric
from .messages import FieldDef, MessageDef, label_of
from .translations import translation_key

# float types: numeric, but with no natural raw range (a control needs bounds)
FLOAT_TYPES: Final[frozenset[str]] = frozenset({"EXP", "EXR"})
# a message named so holds the total of a closed period (last month, last year):
# it changes once per period, so its history is a series of totals, not a meter
_PERIOD_SUFFIXES: Final = ("LastMonth", "LastYear")

CacheKey: TypeAlias = tuple[str, str, str]  # (device, message, label)


def value_key(msg: MessageDef, label: str) -> CacheKey:
    """Cache key for one decoded value: the device (address if known, else
    circuit), the message name and the value's label."""
    return (msg.zz or msg.circuit, msg.name, label)


@dataclass
class Value:
    """One value of a message: its field, and the label it is decoded under
    (:meth:`MessageDef.decode_labeled`)."""
    msg: MessageDef
    field: FieldDef
    label: str

    @property
    def key(self) -> str:
        """Stable, language-neutral key (e.g. a translation key)."""
        return translation_key(self.msg, self.field)

    @property
    def cache_key(self) -> CacheKey:
        """Where the engine keeps this value (see :func:`value_key`)."""
        return value_key(self.msg, self.label)


def values_of(msg: MessageDef) -> list[Value]:
    """One value per named, non-ignored field, labelled as decoded."""
    return [Value(msg, f, label_of(msg.name, f.name)) for f in msg.value_fields]


def closes_period(msg: MessageDef) -> bool:
    """Whether the message holds the total of a closed period (last month or
    year) rather than a running meter."""
    return msg.name.endswith(_PERIOD_SUFFIXES)


def _base(type_: str) -> str:
    return type_.upper().split(":", 1)[0]


def is_number_type(type_: str) -> bool:
    """A type a number control can write (integers, and floats given bounds)."""
    return is_numeric(type_) or _base(type_) in FLOAT_TYPES


def is_float_type(type_: str) -> bool:
    return _base(type_) in FLOAT_TYPES


def has_numeric_value(type_: str) -> bool:
    """A type that decodes to a number (so its history can be aggregated)."""
    return is_number_type(type_) or _base(type_) == "BCD"


def control_of(msg: MessageDef) -> tuple[Literal["number", "select"], Value] | None:
    """The control an active write message allows: a select for a value table, a
    number for a numeric type; only single-value writes get one."""
    if msg.direction != "w":
        return None
    values = values_of(msg)
    if len(values) != 1:
        return None
    value = values[0]
    if value.field.values:
        return "select", value
    if is_number_type(value.field.type):
        return "number", value
    return None
