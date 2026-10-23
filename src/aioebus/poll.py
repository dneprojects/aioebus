"""Poll scheduling under limited bus bandwidth (Phase 4).

eBUS runs at 2400 baud -- a read transaction is ~25 bytes, so only a handful fit
per second. With hundreds of registers you cannot poll everything, so the
scheduler follows three rules, the same shape ebusd uses:

1. Only ``r1``..``r9`` messages are polled at all; plain ``r`` is read on demand
   and ``w``/``u`` never. The priority digit sets how often: the effective
   interval is ``priority * base_interval`` (``r1`` most frequent).
2. Passive-first: a value seen on the bus (a controller broadcast) refreshes the
   same register's timer via :meth:`mark`, so it is not polled redundantly. On a
   real system this removes most active traffic (see the live listen test).
3. One request at a time: the bus is a single shared line, so the scheduler only
   ever hands back the single most-overdue message; the caller paces the reads.

The scheduler is pure and clock-injected (``now`` is passed in), so it is fully
unit-testable without a bus or wall-clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .messages import MessageDef


@dataclass
class PollItem:
    msg: MessageDef
    priority: int
    last_update: float = field(default=0.0)  # last value time (poll OR passive)


class PollScheduler:
    def __init__(self, messages: list[MessageDef], base_interval: float = 60.0):
        self.base_interval = base_interval
        self._items: dict[tuple[str, str], PollItem] = {}
        for m in messages:
            if m.poll_priority > 0:  # only r1..r9 are auto-polled
                self._items[(m.circuit, m.name)] = PollItem(m, m.poll_priority)

    def interval(self, item: PollItem) -> float:
        return self.base_interval * item.priority

    def mark(self, circuit: str, name: str, now: float) -> None:
        """Record that a fresh value arrived (from a poll or passively)."""
        item = self._items.get((circuit, name))
        if item is not None:
            item.last_update = now

    def mark_passive(self, msg: MessageDef, now: float) -> None:
        self.mark(msg.circuit, msg.name, now)

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
