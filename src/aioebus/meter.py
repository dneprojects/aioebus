"""Bus load over the last minute: how full the line is, how often we poll (per
device), and how often other masters' reads stay unanswered.

The last is the best sign that we crowd a device out: a controller its gateway
reads all the time answers the gateway less when we poll it too much.

Pure and clock-injected (``now`` is passed in), like the poll scheduler.
"""
from __future__ import annotations

from collections import deque
from typing import Final

from .frame import SYN

WINDOW: Final = 60.0  # seconds the rates and the load are taken over


class BusMeter:
    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        # per whole second: [data symbols, SYN symbols]
        self._symbols: deque[tuple[int, list[int]]] = deque()
        self._polls: deque[tuple[float, int]] = deque()  # our polls: time, address
        self._unanswered: deque[float] = deque()  # other masters' unanswered reads
        self._start: float | None = None

    def symbol(self, byte: int, now: float) -> None:
        """One symbol seen on the bus (any sender, SYN included)."""
        if self._start is None:
            self._start = now
        second = int(now)
        if not self._symbols or self._symbols[-1][0] != second:
            self._symbols.append((second, [0, 0]))
        self._symbols[-1][1][1 if byte == SYN else 0] += 1
        self._trim(now)

    def poll(self, zz: int, now: float) -> None:
        """One of our scheduled polls finished (answered or not)."""
        self._polls.append((now, zz))
        self._trim(now)

    def foreign_unanswered(self, now: float) -> None:
        """Another master's read stayed unanswered (NAK or no answer)."""
        self._unanswered.append(now)
        self._trim(now)

    def utilization(self, now: float) -> float | None:
        """Share of the last minute's symbols that carried data, in percent;
        None before any symbol arrived."""
        self._trim(now)
        data = sum(c[0] for _, c in self._symbols)
        idle = sum(c[1] for _, c in self._symbols)
        if data + idle == 0:
            return None
        return round(100.0 * data / (data + idle), 1)

    def polls_per_minute(self, now: float, zz: int | None = None) -> float | None:
        """Our polls in the last minute (only those to ``zz`` if given)."""
        self._trim(now)
        count = sum(1 for _, a in self._polls if zz is None or a == zz)
        return self._per_minute(count, now)

    def unanswered_per_minute(self, now: float) -> float | None:
        """Other masters' reads that stayed unanswered in the last minute."""
        self._trim(now)
        return self._per_minute(len(self._unanswered), now)

    def _per_minute(self, count: int, now: float) -> float | None:
        if self._start is None:
            return None
        # right after the start the window is shorter: scale up to a minute
        span = min(self.window, max(now - self._start, 1.0))
        return round(count * 60.0 / span, 1)

    def _trim(self, now: float) -> None:
        edge = now - self.window
        while self._symbols and self._symbols[0][0] < int(edge):
            self._symbols.popleft()
        while self._polls and self._polls[0][0] < edge:
            self._polls.popleft()
        while self._unanswered and self._unanswered[0] < edge:
            self._unanswered.popleft()
