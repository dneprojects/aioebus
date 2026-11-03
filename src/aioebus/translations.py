"""Display texts: stable, language-neutral keys, and the texts themselves.

A definition stays language-neutral; an application that shows its values in
several languages looks the display text up by a stable key per field (and its
state labels by their slug). This module derives those keys, so a new device's
definition yields the exact set of keys to translate, and attaches the texts of
the per-language ebusd CSVs to a device's messages (``tr``).
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


def _msg_key(m: MessageDef) -> tuple[str, str, str, str]:
    return (m.direction, m.pbsb, m.msgid, m.name)


def attach_translations(
    defs: list[MessageDef], by_lang: dict[str, list[MessageDef]]
) -> list[MessageDef]:
    """Set ``tr`` on each def from the same message in every provided language.

    ``by_lang`` maps a language code to that language's parsed definitions of the
    *same device* (the per-language CSVs are structurally identical). Messages are
    matched by ``(direction, pbsb, id, name)`` and fields by position (guarded by
    the field name), so order differences do not matter. Only non-empty comments
    are stored; a message/field with no translation anywhere keeps ``tr=None``.
    Mutates and returns ``defs``."""
    # conditional variants share a key: each takes its own variant's texts
    # (same conditions), else the first one's
    index: dict[str, dict] = {}
    for lang, msgs in by_lang.items():
        for m in msgs:
            idx = index.setdefault(lang, {})
            idx.setdefault(_msg_key(m), m)
            idx.setdefault((*_msg_key(m), tuple(m.conditions or ())), m)
    for m in defs:
        key = (*_msg_key(m), tuple(m.conditions or ()))
        m_tr = {lang: o.comment for lang, idx in index.items()
                if (o := idx.get(key)) is not None and o.comment}
        m.tr = m_tr or None
        for i, f in enumerate(m.fields):
            f_tr = {
                lang: o.fields[i].comment
                for lang, idx in index.items()
                if (o := idx.get(key)) is not None
                and i < len(o.fields) and o.fields[i].name == f.name and o.fields[i].comment
            }
            f.tr = f_tr or None
    return defs
