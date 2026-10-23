"""Passive decoding -- the closed loop: raw wire bytes -> decoded named values.

This is the read-only half of Phase 3: given a set of `MessageDef`s (from a CSV
or the native JSON) and telegrams seen on the bus, identify each telegram and
decode it, with no bus writes and so no risk to the installation. The active
side (polling, writes, scan) is Phase 4.

A telegram is matched to a definition by its command bytes (PBSB), its message
id (the leading master-data bytes), and -- when the definition pins one -- the
destination address. The master id prefix is stripped before decoding so write
payloads line up the same way read responses do.
"""
from __future__ import annotations

from .conditions import ConditionContext, evaluate_all
from .frame import Telegram, parse_telegram
from .messages import MessageDef


class MessageRegistry:
    """Indexes message definitions for fast lookup from a live telegram.

    With a :class:`ConditionContext` (e.g. built from a device scan), a message
    whose conditions evaluate to False is excluded, and a definitely-active one
    is preferred over one whose conditions are unknown."""

    def __init__(self, defs: list[MessageDef], context: ConditionContext | None = None):
        self._context = context
        self._by_pbsb: dict[str, list[MessageDef]] = {}
        for d in defs:
            self._by_pbsb.setdefault(d.pbsb.lower(), []).append(d)

    @property
    def messages(self) -> list[MessageDef]:
        """Every definition the registry holds (flattened), in insertion order."""
        return [d for defs in self._by_pbsb.values() for d in defs]

    def _condition_score(self, d: MessageDef) -> int | None:
        """2 = active/unconditional, 1 = unknown, None = excluded (False)."""
        if not d.conditions:
            return 2
        if self._context is None:
            return 1  # cannot evaluate without a context; keep but deprioritise
        result = evaluate_all(d.conditions, self._context)
        return None if result is False else (2 if result else 1)

    def match(self, tg: Telegram) -> MessageDef | None:
        """Find the definition for a telegram, preferring the longest matching
        message id (so a specific sub-command wins over a generic prefix), then
        the one whose conditions best fit the context."""
        best: MessageDef | None = None
        best_rank = (-1, -1)
        for d in self._by_pbsb.get(tg.pbsb, ()):
            mid = bytes.fromhex(d.msgid) if d.msgid else b""
            if not tg.data.startswith(mid):
                continue
            if d.zz and len(d.zz) == 2 and int(d.zz, 16) != tg.zz:
                continue
            cond = self._condition_score(d)
            if cond is None:  # conditions evaluate to False -> not this device
                continue
            rank = (len(mid), cond)
            if rank > best_rank:
                best, best_rank = d, rank
        return best

    def decode(self, tg: Telegram) -> tuple[MessageDef, dict] | None:
        """Match and decode one telegram into HA-style labelled values."""
        d = self.match(tg)
        if d is None:
            return None
        mid_len = len(bytes.fromhex(d.msgid)) if d.msgid else 0
        values = d.decode_labeled(master=tg.data[mid_len:], slave=tg.slave_data)
        return d, values


def decode_raw(registry: MessageRegistry, raw: bytes | str):
    """Convenience: parse a wire frame and decode it, or None if unknown/bad."""
    tg = parse_telegram(raw)
    if not tg.crc_ok:
        return None
    return registry.decode(tg)
