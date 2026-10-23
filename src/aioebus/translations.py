"""Bridge from message definitions to Home Assistant's translation system.

HA does not localise an entity by its name string; it gives the entity a stable
``translation_key`` and looks the display name (and the labels of its states) up
in ``translations/<lang>.json``. So the definition stays language-neutral -- the
German wording lives in the translation files, maintained separately from the
protocol knowledge.

This module derives those stable keys and emits the English base file
(``strings.json`` in HA terms). A ``de.json`` is a translated copy of it: same
keys, German values. Keeping the keys generated (not hand-written) means a new
device's config immediately yields the exact set of keys to translate.
"""
from __future__ import annotations

import re

from .messages import FieldDef, MessageDef

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.lower()).strip("_")


def translation_key(msg: MessageDef, field: FieldDef) -> str:
    """Stable HA translation_key for one decodable field. A lone generic
    ``value`` field keys off the message name alone (matching the HA-style label
    from :meth:`MessageDef.decode_labeled`); any other field is suffixed."""
    base = _slug(msg.name)
    if field.name and field.name != "value":
        return f"{base}_{_slug(field.name)}"
    return base


def _value_fields(msg: MessageDef):
    for f in msg.fields:
        if f.name and not f.type.upper().startswith("IGN"):
            yield f


def ha_strings(defs: list[MessageDef], platform: str = "sensor") -> dict:
    """Build the HA ``strings.json`` base (English/source) for a device's defs:
    one entry per decodable field, with a ``state`` map wherever the field has a
    value table. Copy to ``de.json`` and translate the values, keeping the keys.
    """
    entities: dict[str, dict] = {}
    for msg in defs:
        for field in _value_fields(msg):
            key = translation_key(msg, field)
            entry: dict = {"name": msg.name if field.name == "value" else f"{msg.name} {field.name}"}
            if field.values:
                entry["state"] = {_slug(label): label for label in field.values.values()}
            entities[key] = entry
    return {"entity": {platform: entities}}
