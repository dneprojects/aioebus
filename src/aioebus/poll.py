"""Poll scheduling under limited bus bandwidth (Phase 4).

eBUS runs at 2400 baud -- a read transaction is ~25 bytes, so only a handful fit
per second. The scheduler follows three rules, the same shape ebusd uses:

1. Which reads are polled, and how often, follows the priority. An ``r1``..``r9``
   message carries its own digit (``r1`` most frequent); a plain ``r`` has no
   digit and is polled only if a ``default_priority`` is given (otherwise it is
   read on demand, as in ebusd). ``w``/``u`` are never polled. The effective
   interval is ``priority * base_interval``, so a larger digit means rarer -- a
   plain read given a high default priority is the rarest, which suits the mostly
   static registers it covers.
2. Passive-first: a value seen on the bus (any master reading it, a controller
   broadcast) refreshes that register's timer via :meth:`mark`, so it is not
   polled redundantly. On a real system this removes most active traffic -- only
   the otherwise-silent registers (e.g. a controller's own) are actually read.
3. One request at a time: the bus is a single shared line, so the scheduler only
   ever hands back the single most-overdue message; the caller paces the reads.

Registers are keyed per device by ``(zz, name)``, so the same register name on
two devices is scheduled -- and suppressed passively -- independently.

The scheduler is pure and clock-injected (``now`` is passed in), so it is fully
unit-testable without a bus or wall-clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .messages import MessageDef

if TYPE_CHECKING:
    from .passive import MessageRegistry


@dataclass
class PollItem:
    msg: MessageDef
    priority: int
    last_update: float = field(default=0.0)  # last value time (poll OR passive)


class PollScheduler:
    def __init__(self, messages: list[MessageDef], base_interval: float = 60.0,
                 default_priority: int = 0):
        """``default_priority`` is the priority given to plain ``r`` reads (0 =
        do not poll them, ebusd's read-on-demand default; a positive value polls
        every ``default_priority * base_interval``). ``r1``..``r9`` keep their
        own digit regardless."""
        self.base_interval = base_interval
        self.default_priority = default_priority
        self._items: dict[tuple[str, str], PollItem] = {}
        for m in messages:
            if m.direction[:1] != "r":  # only true reads are polled (not w/u)
                continue
            priority = m.poll_priority or default_priority
            if priority > 0:
                self._items[(m.zz, m.name)] = PollItem(m, priority)

    @classmethod
    def for_registry(cls, registry: MessageRegistry, base_interval: float = 60.0,
                     default_priority: int = 0) -> PollScheduler:
        """Build a scheduler over every readable message a registry knows -- the
        library's own 'poll what is readable' policy, so a consumer need not
        assemble the list itself."""
        return cls(registry.messages, base_interval=base_interval,
                   default_priority=default_priority)

    def interval(self, item: PollItem) -> float:
        return self.base_interval * item.priority

    def mark(self, zz: str, name: str, now: float) -> None:
        """Record that a fresh value arrived (from a poll or passively)."""
        item = self._items.get((zz, name))
        if item is not None:
            item.last_update = now

    def mark_passive(self, msg: MessageDef, now: float) -> None:
        self.mark(msg.zz, msg.name, now)

    def next_due(self, now: float) -> PollItem | None:
        """The single most-overdue message to read now, or None if none is due.
        Overdue-ness is measured relative to each item's own interval, so a
        high-priority item that is only slightly late can still beat a low-priority
        one; ties break toward the higher priority (lower number)."""
        best: PollItem | None = None
        best_key: tuple[float, int] | None = None
        for item in self._items.values():
            score = (now - item.last_update) / self.interval(item)
            if score < 1.0:  # interval not yet elapsed -> not due
                continue
            key = (score, -item.priority)  # most overdue, then highest priority
            if best_key is None or key > best_key:
                best, best_key = item, key
        return best

    def due_count(self, now: float) -> int:
        return sum(
            1 for it in self._items.values()
            if (now - it.last_update) >= self.interval(it)
        )
