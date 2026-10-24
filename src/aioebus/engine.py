"""Unified protocol engine: passive-first, with active transactions interleaved.

One connection to the adapter, one stream decoder. The engine continuously
assembles and decodes the telegrams other participants put on the bus (passive,
free of charge) and, only when the bus is idle, slips in its own active
transactions -- scheduled polls, explicit reads, or writes. A value seen
passively refreshes the poll scheduler, so broadcast data is never re-polled.

This is the piece that lets a thin Home Assistant integration get most values for
free and spend the scarce 2400-baud bandwidth only on the rest.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .active import DEFAULT_SOURCE, ReadDriver, ReadError, WriteDriver, _CommandDriver
from .frame import SYN, FrameError, parse_telegram
from .messages import MessageDef
from .passive import MessageRegistry
from .poll import PollScheduler
from .transport import RECEIVED, EnhDecoder


def value_key(msg: MessageDef, field: str) -> tuple[str, str, str]:
    """Cache key for one decoded field: the device (address if known, else
    circuit), the message name and the field label."""
    return (msg.zz or msg.circuit, msg.name, field)


@dataclass
class _Active:
    make: Callable[[], _CommandDriver]  # builds a fresh driver (they are single-shot)
    msg: MessageDef | None  # message being read/polled (for storing the result)
    future: asyncio.Future | None  # set for explicit read/write requests
    attempts: int = 0
    driver: _CommandDriver | None = None


@dataclass
class EngineStats:
    """Live counters for the adapter link, meant to be surfaced as the adapter's
    own diagnostic entities (connection, bus activity, poll success)."""

    connected: bool = False  # the stream is up and bytes are arriving
    telegrams: int = 0  # telegrams decoded off the bus passively (free)
    polls_ok: int = 0  # scheduled polls that got an answer
    polls_failed: int = 0  # scheduled polls that stayed unanswered (backed off)
    writes: int = 0  # active writes performed
    last_telegram: float | None = None  # monotonic time of the last bus telegram


class EbusEngine:
    """Drive one eBUS adapter connection: passive decode + interleaved actives."""

    MAX_ATTEMPTS = 12  # arbitration retries for an explicit request

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        recv: Callable[[], Awaitable[bytes]],
        registry: MessageRegistry,
        *,
        scheduler: PollScheduler | None = None,
        qq: int = DEFAULT_SOURCE,
        on_value: Callable[[MessageDef, dict], None] | None = None,
        allow_write: bool = False,
    ):
        self._send = send
        self._recv = recv
        self.registry = registry
        self.scheduler = scheduler
        self.qq = qq
        self._on_value = on_value
        self._allow_write = allow_write
        self._dec = EnhDecoder()
        self._buf = bytearray()  # passive telegram assembly (on-wire bytes)
        self._active: _Active | None = None
        self._queue: deque[_Active] = deque()
        self.cache: dict[tuple[str, str, str], object] = {}
        self.stats = EngineStats()
        self._stop = False

    # -- public API ---------------------------------------------------------

    async def run(self) -> None:
        """Main loop until stop(); pumps the adapter stream."""
        while not self._stop:
            try:
                chunk = await self._recv()
            except (OSError, asyncio.IncompleteReadError):
                break
            if not chunk:
                break
            self.stats.connected = True
            for code, data in self._dec.feed(chunk):
                await self._on_event(code, data)
        self.stats.connected = False

    def stop(self) -> None:
        self._stop = True
        self.stats.connected = False

    def attach(self, send: Callable[[bytes], Awaitable[None]],
               recv: Callable[[], Awaitable[bytes]]) -> None:
        """Rebind to a fresh connection (after a reconnect) and reset the stream
        state. The value cache and poll scheduler are kept across reconnects."""
        self._send = send
        self._recv = recv
        self._dec = EnhDecoder()
        self._buf.clear()
        self._active = None
        self._stop = False
        self.stats.connected = False

    async def read(self, msg: MessageDef, timeout: float = 4.0) -> dict:
        """Actively read a message as soon as the bus is idle; return its values."""
        self._require_addr(msg)
        zz = int(msg.zz, 16)
        slave = await self._submit(lambda: ReadDriver(zz, msg.pbsb, msg.msgid, self.qq),
                                   msg, timeout)
        return msg.decode_labeled(slave=slave)

    async def write(self, msg: MessageDef, values: dict, timeout: float = 4.0) -> None:
        """Actively write a message (requires allow_write=True)."""
        if not self._allow_write:
            raise PermissionError("writing is disabled; construct the engine with allow_write=True")
        if not msg.is_write:
            raise ValueError(f"{msg.name}: not a write message")
        self._require_addr(msg)
        zz = int(msg.zz, 16)
        payload = msg.encode(values)
        await self._submit(lambda: WriteDriver(zz, msg.pbsb, payload, self.qq), msg, timeout)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _require_addr(msg: MessageDef) -> None:
        if not (msg.zz and len(msg.zz) == 2):
            raise ValueError(f"{msg.name}: no destination address")

    async def _submit(self, make: Callable[[], _CommandDriver], msg: MessageDef, timeout: float):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue.append(_Active(make, msg, fut))
        return await asyncio.wait_for(fut, timeout)

    async def _on_event(self, code: int, data: int) -> None:
        if self._active is not None:
            for sym in self._active.driver.feed_events([(code, data)]):
                await self._send(sym)
            if self._active.driver.done:
                await self._finish_active()
            return

        if code != RECEIVED:
            return
        if data == SYN:
            if self._buf:
                self._passive_telegram(bytes(self._buf))
                self._buf.clear()
            await self._maybe_start_active()  # bus idle: a good moment to act
        else:
            self._buf.append(data)

    def _passive_telegram(self, raw: bytes) -> None:
        try:
            tg = parse_telegram(raw)
        except FrameError:
            return
        if not tg.crc_ok:
            return
        self.stats.telegrams += 1
        self.stats.last_telegram = time.monotonic()
        decoded = self.registry.decode(tg)
        if decoded is not None:
            self._store(*decoded)

    def _store(self, msg: MessageDef, values: dict) -> None:
        for field, value in values.items():
            self.cache[value_key(msg, field)] = value
        if self.scheduler is not None:
            self.scheduler.mark_passive(msg, time.monotonic())
        if self._on_value is not None:
            self._on_value(msg, values)

    async def _maybe_start_active(self) -> None:
        if self._active is not None or self._buf:
            return
        if self._queue:
            act = self._queue.popleft()
        elif self.scheduler is not None:
            item = self.scheduler.next_due(time.monotonic())
            if item is None:
                return
            msg = item.msg
            zz = int(msg.zz, 16)
            act = _Active(lambda: ReadDriver(zz, msg.pbsb, msg.msgid, self.qq), msg, None)
        else:
            return
        act.driver = act.make()
        act.attempts += 1
        self._active = act
        for sym in act.driver.start():
            await self._send(sym)

    async def _finish_active(self) -> None:
        act = self._active
        self._active = None
        drv = act.driver
        if drv.error is None:
            if isinstance(drv, ReadDriver) and act.msg is not None:
                self._store(act.msg, act.msg.decode_labeled(slave=drv.result))
                if act.future is None:  # a scheduled poll, not an explicit read
                    self.stats.polls_ok += 1
            elif isinstance(drv, WriteDriver):
                self.stats.writes += 1
            if act.future is not None and not act.future.done():
                act.future.set_result(drv.result)
            return
        # We lost the bus to another master: capture ITS telegram passively instead
        # of discarding it. The arbitration consumed only the winner's address byte
        # (reported as drv.lost_to); the rest of its telegram is still arriving, and
        # now that _active is clear those bytes route to the passive buffer -- so
        # seed the buffer with the winner's address and the next SYN decodes the
        # whole telegram (a broadcast, or another master's read incl. the slave part).
        lost_to = getattr(drv, "lost_to", None)
        if isinstance(lost_to, int) and not self._buf:
            self._buf.append(lost_to)
        # error: retry a lost/contended bus (rebuild a fresh driver), else give up
        retryable = "arbitration lost" in drv.error or "collision" in drv.error
        if retryable and act.attempts < self.MAX_ATTEMPTS:
            self._queue.appendleft(act)  # make() runs again on the next idle
            return
        if act.future is not None and not act.future.done():
            act.future.set_exception(ReadError(drv.error))
        elif act.msg is not None and self.scheduler is not None:
            # a poll the device did not answer (NAK/timeout): record the attempt
            # and back it off. Without recording it, its timer stays at zero so it
            # is forever the most-overdue item and the scheduler keeps re-handing
            # the same dead register, starving the rest; the backoff then fades a
            # persistently unanswered register out of the rotation entirely.
            self.scheduler.mark_failure(act.msg, time.monotonic())
            self.stats.polls_failed += 1
