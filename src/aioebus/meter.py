"""Bus load over the last minute: how full the line is, how often we poll (per
device), how much each other master sends, how often other masters' reads stay
unanswered, and how often telegrams are not acknowledged (see acks.py).

The last is the best sign that we crowd a device out: a controller its gateway
reads all the time answers the gateway less when we poll it too much.

Pure and clock-injected (``now`` is passed in), like the poll scheduler.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Collection
from typing import Final

from .frame import SYN

WINDOW: Final = 60.0  # seconds the rates and the load are taken over
# symbols the bus carries at most: 2400 baud, 10 bits a symbol (start, 8, stop).
# The load is measured against this, not against the SYN seen: an adapter need
# not pass every idle SYN on.
SYMBOLS_PER_SECOND: Final = 240.0


class BusMeter:
    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        # per whole second: [data symbols, SYN symbols]
        self._symbols: deque[tuple[int, list[int]]] = deque()
        self._polls: deque[tuple[float, int]] = deque()  # our polls: time, address
        self._unanswered: deque[tuple[float, int]] = deque()  # others' unanswered reads
        self._sent: deque[tuple[float, int]] = deque()  # others' telegrams: time, sender
        self.senders: set[int] = set()  # every other master seen sending (ever)
        # others' telegrams with a failed acknowledgment: time, address, lost
        self._ack_faults: deque[tuple[float, int, bool]] = deque()
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

    def foreign_unanswered(self, zz: int, now: float) -> None:
        """Another master's read of the device at ``zz`` stayed unanswered (NAK
        or no answer)."""
        self._unanswered.append((now, zz))
        self._trim(now)

    def sent(self, qq: int, now: float) -> None:
        """Another master at ``qq`` sent a telegram (any kind, broadcasts too)."""
        self._sent.append((now, qq))
        self.senders.add(qq)
        self._trim(now)

    def sent_per_minute(self, now: float, qq: int) -> float | None:
        """The telegrams the master at ``qq`` sent in the last minute."""
        self._trim(now)
        return self._per_minute(sum(1 for _, a in self._sent if a == qq), now)

    def ack_fault(self, zz: int, lost: bool, now: float) -> None:
        """Another master's telegram to ``zz`` was not acknowledged cleanly;
        ``lost`` when it did not arrive at all."""
        self._ack_faults.append((now, zz, lost))
        self._trim(now)

    def utilization(self, now: float) -> float | None:
        """The share of the bus's capacity the last minute's data used, in
        percent; None before any symbol arrived."""
        self._trim(now)
        if self._start is None:
            return None
        data = sum(c[0] for _, c in self._symbols)
        span = min(self.window, max(now - self._start, 1.0))
        return round(min(100.0, 100.0 * data / (span * SYMBOLS_PER_SECOND)), 1)

    def polls_per_minute(self, now: float, zz: int | None = None) -> float | None:
        """Our polls in the last minute (only those to ``zz`` if given)."""
        self._trim(now)
        count = sum(1 for _, a in self._polls if zz is None or a == zz)
        return self._per_minute(count, now)

    def unanswered_per_minute(self, now: float,
                              addresses: Collection[int] | None = None) -> float | None:
        """Other masters' reads that stayed unanswered in the last minute; only
        those to ``addresses`` if given (devices that are there: a controller
        probing an empty address is no sign of trouble)."""
        self._trim(now)
        count = sum(1 for _, zz in self._unanswered if addresses is None or zz in addresses)
        return self._per_minute(count, now)

    def ack_faults_per_minute(self, now: float, addresses: Collection[int] | None = None,
                              *, lost_only: bool = False) -> float | None:
        """Other masters' telegrams with a failed acknowledgment in the last
        minute; only those to ``addresses`` if given, only lost ones if asked."""
        self._trim(now)
        count = sum(1 for _, zz, lost in self._ack_faults
                    if (addresses is None or zz in addresses) and (lost or not lost_only))
        return self._per_minute(count, now)

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
        while self._unanswered and self._unanswered[0][0] < edge:
            self._unanswered.popleft()
        while self._sent and self._sent[0][0] < edge:
            self._sent.popleft()
        while self._ack_faults and self._ack_faults[0][0] < edge:
            self._ack_faults.popleft()
