"""What a message's values are: one :class:`Value` per named field, its stable
key and label, whether it is numeric, and the control a writable one allows.

Presentation (entity platforms, names, icons) is the consumer's business; this
module only states the facts every consumer derives it from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from .codecs import is_numeric
from .messages import FieldDef, MessageDef
from .translations import translation_key

# float types: numeric, but with no natural raw range (a control needs bounds)
FLOAT_TYPES: Final[frozenset[str]] = frozenset({"EXP", "EXR"})


@dataclass
class Value:
    """One value of a message: its field, and the label it is cached under
    (:meth:`MessageDef.decode_labeled`)."""
    msg: MessageDef
    field: FieldDef
    label: str

    @property
    def key(self) -> str:
        """Stable, language-neutral key (e.g. a translation key)."""
        return translation_key(self.msg, self.field)


def values_of(msg: MessageDef) -> list[Value]:
    """One value per named, non-ignored field. A lone generic ``value`` field is
    labelled with the message name, any other with ``"<message> <field>"``."""
    fields = msg.value_fields
    lone_value = [f.name for f in fields] == ["value"]
    return [Value(msg, f, msg.name if (f.name == "value" or lone_value)
                  else f"{msg.name} {f.name}") for f in fields]


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
    """The control a write message allows: a select for a value table, a number
    for a numeric type; only single-value writes get one."""
    if not msg.is_write:
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
