"""Bridge from message definitions to Home Assistant's translation system.

HA does not localise an entity by its name string; it gives the entity a stable
``translation_key`` and looks the display name (and the labels of its states) up
in ``translations/<lang>.json``. So the definition stays language-neutral -- the
German wording lives in the translation files, maintained separately from the
protocol knowledge.

This module derives those stable keys; the consumer (the ebus integration)
builds its translation files from them, so a new device's definition yields the
exact set of keys to translate.
"""
from __future__ import annotations

import re

from .messages import FieldDef, MessageDef

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """Lower-case text with every run of other characters as one underscore (the
    form of translation keys and state keys)."""
    return _SLUG.sub("_", text.lower()).strip("_")


_slug = slug  # former private name, still imported by ebus up to 0.6.4


def localized(tr: dict[str, str] | None, lang: str, fallback: str) -> str:
    """Pick the display text for ``lang`` from an embedded translations map,
    falling back to English, then to ``fallback`` (usually the technical label).

    This is the single resolution used for data-driven entity names: a definition
    carries its own ``tr`` (merged from the per-language CSVs at import), so the
    same code path serves bundled and imported devices, in any installed language.
    """
    if tr:
        text = tr.get(lang) or tr.get("en")
        if text:
            return text
    return fallback


def translation_key(msg: MessageDef, field: FieldDef) -> str:
    """Stable HA translation_key for one decodable field. A lone generic
    ``value`` field keys off the message name alone (matching the HA-style label
    from :meth:`MessageDef.decode_labeled`); any other field is suffixed."""
    base = slug(msg.name)
    if field.name and field.name != "value":
        return f"{base}_{slug(field.name)}"
    return base
