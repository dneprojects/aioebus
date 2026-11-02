"""Stable, language-neutral keys for display texts.

A definition stays language-neutral; an application that shows its values in
several languages looks the display text up by a stable key per field (and its
state labels by their slug). This module derives those keys, so a new device's
definition yields the exact set of keys to translate.
"""
from __future__ import annotations

import re

from .messages import FieldDef, MessageDef

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """Lower-case text with every run of other characters as one underscore (the
    form of translation keys and state keys)."""
    return _SLUG.sub("_", text.lower()).strip("_")


def translation_key(msg: MessageDef, field: FieldDef) -> str:
    """Stable key for one decodable field. A generic ``value`` field keys off
    the message name alone (as its label, :func:`messages.label_of`); any other
    field is suffixed."""
    base = slug(msg.name)
    if field.name and field.name != "value":
        return f"{base}_{slug(field.name)}"
    return base
