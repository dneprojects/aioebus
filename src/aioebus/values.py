"""What a message's values are: one :class:`Value` per named field, its stable
key, label and cache key, and the control a writable one allows (whether a type
is numeric is the codecs' business).

Presentation (entity platforms, names, icons) is the consumer's business; this
module only states the facts every consumer derives it from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from .codecs import is_number
from .messages import FieldDef, MessageDef, label_of
from .translations import translation_key

# a message named so holds the total of a closed period (last month, last year):
# it changes once per period, so its history is a series of totals, not a meter
_PERIOD_SUFFIXES: Final = ("LastMonth", "LastYear")

MessageKey: TypeAlias = tuple[str, str]  # (device, message)
CacheKey: TypeAlias = tuple[str, str, str]  # (device, message, label)


def message_key(msg: MessageDef) -> MessageKey:
    """Which register of which device a message is: the device (address if
    known, else circuit) and the message name."""
    return (msg.zz or msg.circuit, msg.name)


def value_key(msg: MessageDef, label: str) -> CacheKey:
    """Cache key for one decoded value: :func:`message_key` and the value's label."""
    return (*message_key(msg), label)


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


def sent_value(msg: MessageDef) -> Value | None:
    """The one value an active write message sends (a field of the answer is
    not sent); None for any other message."""
    if msg.direction != "w":
        return None
    sent = msg.request_fields()
    values = [v for v in values_of(msg) if v.field in sent]
    return values[0] if len(values) == 1 else None


def control_of(msg: MessageDef) -> tuple[Literal["number", "select"], Value] | None:
    """The control an active write message allows: a select for a value table, a
    number for a numeric type; only writes sending a single value get one."""
    if (value := sent_value(msg)) is None:
        return None
    if value.field.values:
        return "select", value
    if is_number(value.field.type):
        return "number", value
    return None
