"""Passive decoding: raw wire bytes -> decoded named values, with no bus writes.

Given the message definitions of the devices on the bus (the catalog has chosen
each device's variants and stamped its address), identify each telegram seen on
the bus and decode it.

A telegram is matched to a definition by its command bytes (PBSB), its message
id (the leading master-data bytes), and -- when the definition pins one -- the
destination address. A read and a write may share the id: a telegram with data
after the id is the write (or a passive update), one without it the read. The
master id prefix is stripped before decoding so write payloads line
up the same way read responses do.
"""
from __future__ import annotations

from .frame import Telegram, parse_telegram
from .messages import MessageDef


def expects_slave(d: MessageDef) -> bool:
    """True if any field of the definition lives in the slave's answer."""
    default = d.default_part
    return any((f.part if f.part in ("m", "s") else default) == "s" for f in d.fields)


def _has_master_data(d: MessageDef) -> bool:
    """True if the request carries data after the id (a write's value, a read's
    arguments)."""
    default = d.default_part
    return any((f.part if f.part in ("m", "s") else default) == "m" for f in d.fields)


class MessageRegistry:
    """Indexes message definitions for fast lookup from a live telegram."""

    def __init__(self, defs: list[MessageDef]):
        self._by_pbsb: dict[str, list[MessageDef]] = {}
        for d in defs:
            self._by_pbsb.setdefault(d.pbsb.lower(), []).append(d)

    @property
    def messages(self) -> list[MessageDef]:
        """Every definition the registry holds (flattened), in insertion order."""
        return [d for defs in self._by_pbsb.values() for d in defs]

    def match(self, tg: Telegram) -> MessageDef | None:
        """Find the definition for a telegram: the longest matching message id
        (a specific sub-command wins over a generic prefix), then the direction
        that fits it (data after the id: a write, none: a plain read), then a
        passive definition (u/uw: what another master sends) over an active one."""
        best: MessageDef | None = None
        best_rank = (-1, False, False)
        for d in self._by_pbsb.get(tg.pbsb, ()):
            mid = bytes.fromhex(d.msgid) if d.msgid else b""
            if not tg.data.startswith(mid):
                continue
            if d.zz and len(d.zz) == 2 and int(d.zz, 16) != tg.zz:
                continue
            fits = _has_master_data(d) == (len(tg.data) > len(mid))
            rank = (len(mid), fits, d.is_passive)
            if rank > best_rank:
                best, best_rank = d, rank
        return best

    def decode(self, tg: Telegram) -> tuple[MessageDef, dict] | None:
        """Match and decode one telegram into labelled values."""
        d = self.match(tg)
        if d is None:
            return None
        if not tg.slave_data and expects_slave(d):
            # another master's read that went unanswered: there is no value to
            # decode, so do not report one (it would overwrite a good value with
            # None and suppress the poll as if the register had been seen)
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
