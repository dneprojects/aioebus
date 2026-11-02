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
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .active import (
    CANCEL_ARBITRATION,
    DEFAULT_SOURCE,
    NO_ANSWER,
    NO_ANSWER_TRIES,
    RELEASE_BUS,
    ReadDriver,
    ReadError,
    WriteDriver,
    WriteUncertain,
    _CommandDriver,
)
from .frame import SYN, FrameError, parse_telegram
from .messages import MessageDef
from .passive import MessageRegistry, expects_slave
from .poll import PollScheduler
from .scan import (
    IDENT_MSGIDS,
    IDENT_PBSB,
    SCAN_PBSB,
    Identification,
    ScanResult,
    decode_identification,
    decode_scan,
)
from .transport import RECEIVED, STARTED, EnhDecoder

_LOGGER = logging.getLogger(__name__)

def value_key(msg: MessageDef, field: str) -> tuple[str, str, str]:
    """Cache key for one decoded field: the device (address if known, else
    circuit), the message name and the field label."""
    return (msg.zz or msg.circuit, msg.name, field)


@dataclass(eq=False)  # compared by identity (queue removal)
class _Active:
    make: Callable[[], _CommandDriver]  # builds a fresh driver (they are single-shot)
    msg: MessageDef | None  # message being read/polled (for storing the result)
    future: asyncio.Future | None  # set for explicit read/write requests
    attempts: int = 0
    driver: _CommandDriver | None = None
    started: float = 0.0  # monotonic time the current attempt began
    # a read the slave ACKs but leaves unanswered is tried this often in all
    # (some devices skip the odd answer; a read has no side effect)
    answer_tries: int = 1
    no_answers: int = 0
    expires: float | None = None  # monotonic time the caller stops waiting


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
    # one transaction (arbitration to final SYN) must finish within this; a bus
    # that never completes it would otherwise hold the engine in the active state
    # forever, starving passive decoding and every later poll
    ACTIVE_DEADLINE = 3.0
    # an explicit request starts only with this much time to spare beyond the
    # deadline, so its last bytes and ACK land before the caller stops waiting
    START_MARGIN = 0.2

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
        self.running = False  # run() is pumping the stream
        self._traffic = asyncio.Event()  # set by the first bus symbol
        self._ended = asyncio.Event()  # set when run() ends

    # -- public API ---------------------------------------------------------

    async def run(self) -> None:
        """Main loop until stop() or the link ends; pumps the adapter stream. A
        failure while handling one event fails only the transaction in flight."""
        self.running = True
        self._ended.clear()
        try:
            while not self._stop:
                try:
                    chunk = await self._recv()
                except (OSError, asyncio.IncompleteReadError):
                    break
                if not chunk:
                    break
                self.stats.connected = True
                for code, data in self._dec.feed(chunk):
                    if code == RECEIVED:
                        self._traffic.set()
                    try:
                        await self._on_event(code, data)
                    except OSError:  # sending failed: the link is gone
                        return
                    except Exception as err:  # keep the bus loop alive
                        _LOGGER.exception("eBUS engine: event handling failed")
                        try:
                            await self._abandon_active(f"internal error: {err}")
                        except OSError:  # the cancel could not be sent: link gone
                            return
        finally:
            self.running = False
            self._ended.set()
            self.stats.connected = False
            # the caller of a transaction in flight learns now, not at its timeout
            self._fail_active("connection lost")

    async def wait_for_traffic(self, timeout: float) -> bool:
        """Whether the bus delivers a symbol within ``timeout`` s (a live bus
        streams SYN continuously; silence means no bus power or a dead link).
        Returns early when run() ends meanwhile (the link dropped)."""
        waits = [asyncio.ensure_future(self._traffic.wait()),
                 asyncio.ensure_future(self._ended.wait())]
        try:
            await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        return self._traffic.is_set()

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
        self._traffic = asyncio.Event()
        self._ended.clear()  # the next run() is the live one
        self._fail_active("connection lost")
        self._stop = False
        self.stats.connected = False

    def _fail_active(self, reason: str) -> None:
        """Drop the transaction in flight and fail its caller, telling whether a
        write may already have been applied (so it is not blindly retried)."""
        act, self._active = self._active, None
        if act is None:
            return
        drv = act.driver
        if act.future is None:
            # a scheduled poll: back it off like an unanswered one, or it stays the
            # most overdue register and is restarted at every idle bus
            if act.msg is not None and self.scheduler is not None:
                self.scheduler.mark_failure(act.msg, time.monotonic())
                self.stats.polls_failed += 1
            return
        if act.future.done():
            return
        if isinstance(drv, WriteDriver) and drv.accepted:
            self.stats.writes += 1  # the destination ACKed it: applied
            act.future.set_result(True)
        elif isinstance(drv, WriteDriver) and drv.may_have_landed:
            act.future.set_exception(WriteUncertain(f"{reason} after sending"))
        else:
            act.future.set_exception(ReadError(reason))

    async def _abandon_active(self, reason: str) -> None:
        """Fail the transaction in flight on a live link; a START the adapter still
        holds is withdrawn so it cannot win the bus later."""
        act = self._active
        if act is not None and act.driver is not None and act.driver.arbitrating:
            await self._send(CANCEL_ARBITRATION)
        self._fail_active(reason)

    async def read(self, msg: MessageDef, timeout: float = 6.0) -> dict:
        """Actively read a message as soon as the bus is idle; return its values."""
        if not msg.is_polled_read:
            raise ValueError(f"{msg.name}: not a readable message")
        self._require_addr(msg)
        zz = int(msg.zz, 16)
        slave = await self._submit(lambda: ReadDriver(zz, msg.pbsb, msg.msgid, self.qq),
                                   msg, timeout, answer_tries=NO_ANSWER_TRIES)
        return msg.decode_labeled(slave=slave)

    async def read_raw(self, zz: int, pbsb: str, msgid: str, timeout: float = 6.0) -> bytes:
        """Actively read a register without a definition; return the slave's raw
        answer data (e.g. for the identification reads at runtime)."""
        # malformed arguments fail this call, not later inside the bus loop
        if len(pbsb) != 4:
            raise ValueError(f"{pbsb!r}: PBSB must be 4 hex digits")
        return await self._submit(lambda: ReadDriver(zz, pbsb, msgid, self.qq), None, timeout,
                                  answer_tries=NO_ANSWER_TRIES)

    async def scan(self, zz: int, timeout: float = 6.0) -> ScanResult:
        """Identify the device at an address (0704: manufacturer, id, SW, HW).
        Raises ReadError/TimeoutError when nothing answers there."""
        answer = await self.read_raw(zz, SCAN_PBSB, "", timeout)
        try:
            return decode_scan(answer)
        except ValueError as err:
            raise ReadError(str(err)) from err

    async def read_identification(self, zz: int, timeout: float = 6.0) -> Identification | None:
        """The Vaillant extended identification (article/serial) of a device, read
        through the running engine; None when it does not answer these reads (or
        the link dropped meanwhile), so the caller may simply try again later."""
        try:
            parts = [await self.read_raw(zz, IDENT_PBSB, msgid, timeout)
                     for msgid in IDENT_MSGIDS]
        except (ReadError, TimeoutError):
            return None
        return decode_identification(*parts)

    async def write(self, msg: MessageDef, values: dict, timeout: float = 6.0) -> None:
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

    async def _submit(self, make: Callable[[], _CommandDriver], msg: MessageDef | None,
                      timeout: float, answer_tries: int = 1):
        if timeout <= self.ACTIVE_DEADLINE + self.START_MARGIN:
            # it could never start (see _next_request): say so instead of timing out
            raise ValueError(f"timeout {timeout} s is shorter than one transaction "
                             f"({self.ACTIVE_DEADLINE + self.START_MARGIN} s)")
        # drivers are pure: building one checks the frame (address, PBSB, id,
        # length) here, so a malformed request fails this call, not the bus loop
        make()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        act = _Active(make, msg, fut, expires=time.monotonic() + timeout,
                      answer_tries=answer_tries)
        self._queue.append(act)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            # a request the caller gave up on must not go out later (a stale write
            # landing minutes after the user saw it fail)
            if act in self._queue:
                self._queue.remove(act)

    async def _on_event(self, code: int, data: int) -> None:
        if self._active is not None:
            drv = self._active.driver
            if drv.arbitrating and code == RECEIVED:
                # the adapter arbitrates only at a later SYN: another master's
                # telegram passing meanwhile is still worth decoding
                self._passive_byte(data)
            # feed first: a late ACK still counts (an applied write must not be
            # reported as failed); only then give up a transaction past its time
            for sym in drv.feed_events([(code, data)]):
                await self._send(sym)
            if not drv.arbitrating:
                self._buf.clear()  # the bus is decided: what follows is not passive
            if not drv.done and time.monotonic() - self._active.started > self.ACTIVE_DEADLINE:
                if drv.arbitrating:
                    await self._send(CANCEL_ARBITRATION)
                drv._fail("timeout")
            if drv.done:
                await self._finish_active()
            return

        if code == STARTED:
            # a withdrawn arbitration won anyway (its cancel came too late): the
            # bus is ours with nothing to send, so release it at once
            await self._send(RELEASE_BUS)
            return
        if code != RECEIVED:
            return
        if self._passive_byte(data):
            await self._maybe_start_active()  # bus idle: a good moment to act

    def _passive_byte(self, data: int) -> bool:
        """Assemble passive telegrams; True on a SYN (the bus is idle)."""
        if data != SYN:
            self._buf.append(data)
            return False
        if self._buf:
            self._passive_telegram(bytes(self._buf))
            self._buf.clear()
        return True

    def _passive_telegram(self, raw: bytes) -> None:
        try:
            tg = parse_telegram(raw)
        except FrameError:
            return
        if not tg.crc_ok:
            return
        self.stats.telegrams += 1
        self.stats.last_telegram = time.monotonic()
        try:
            decoded = self.registry.decode(tg)
        except Exception:  # one undecodable telegram, not the bus loop
            _LOGGER.exception("eBUS engine: cannot decode telegram %s", raw.hex())
            return
        if decoded is not None:
            self._store(*decoded)

    def _store(self, msg: MessageDef, values: dict) -> None:
        for field, value in values.items():
            self.cache[value_key(msg, field)] = value
        if self.scheduler is not None:
            self.scheduler.mark_passive(msg, time.monotonic())
        if self._on_value is not None:
            try:
                self._on_value(msg, values)
            except Exception:  # a consumer's bug must not stop the bus
                _LOGGER.exception("eBUS engine: on_value callback failed")

    async def _maybe_start_active(self) -> None:
        if self._active is not None or self._buf:
            return
        act = self._next_request()
        if act is None:
            if self.scheduler is None:
                return
            item = self.scheduler.next_due(time.monotonic())
            if item is None:
                return
            msg = item.msg
            zz = int(msg.zz, 16)
            act = _Active(lambda: ReadDriver(zz, msg.pbsb, msg.msgid, self.qq), msg, None)
            try:
                act.driver = act.make()
            except ValueError:  # a malformed definition: back it off, never crash
                _LOGGER.warning("eBUS engine: cannot poll %s (bad PBSB/id)", msg.name)
                self.scheduler.mark_failure(msg, time.monotonic())
                return
        else:
            act.driver = act.make()
        act.attempts += 1
        act.started = time.monotonic()
        self._active = act
        for sym in act.driver.start():
            await self._send(sym)

    def _next_request(self) -> _Active | None:
        """The next explicit request still worth starting. One whose caller gave up
        is dropped, and so is one that could no longer finish before its caller's
        timeout (a write must never land after the caller saw it fail)."""
        now = time.monotonic()
        while self._queue:
            act = self._queue.popleft()
            if act.future is not None and act.future.done():
                continue
            if (act.expires is not None
                    and act.expires - now < self.ACTIVE_DEADLINE + self.START_MARGIN):
                if act.future is not None:
                    act.future.set_exception(TimeoutError("no time left to run the request"))
                continue
            return act
        return None

    async def _finish_active(self) -> None:
        act = self._active
        self._active = None
        drv = act.driver
        if (drv.error is None and isinstance(drv, ReadDriver) and act.msg is not None
                and not drv.result and expects_slave(act.msg)):
            # an empty answer to a read that expects data carries no value; treat it
            # like no answer instead of storing None and resetting the backoff
            drv.error = "empty answer"
        if isinstance(drv, WriteDriver) and drv.error is not None and drv.accepted:
            # the destination ACKed the write, so it is applied; only the tail of
            # the telegram (its answer) failed -- report success, never resend
            drv.error, drv.result = None, True
        values = None
        if drv.error is None and isinstance(drv, ReadDriver) and act.msg is not None:
            try:
                values = act.msg.decode_labeled(slave=drv.result)
            except Exception as err:  # fail this read, not the bus loop
                _LOGGER.exception("eBUS engine: cannot decode the answer to %s", act.msg.name)
                drv.error = f"cannot decode the answer: {err}"
        if drv.error is None:
            if values is not None and act.msg is not None:
                self._store(act.msg, values)
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
        if isinstance(drv, WriteDriver) and drv.may_have_landed:
            retryable = False  # the whole command went out: a resend could apply it twice
        abandoned = act.future is not None and act.future.done()  # caller gave up
        if isinstance(drv, ReadDriver) and drv.error == NO_ANSWER:
            act.no_answers += 1
            retryable = act.no_answers < act.answer_tries
        if retryable and not abandoned and act.attempts < self.MAX_ATTEMPTS:
            self._queue.appendleft(act)  # make() runs again on the next idle
            return
        if act.future is not None and not act.future.done():
            uncertain = isinstance(drv, WriteDriver) and drv.may_have_landed
            act.future.set_exception((WriteUncertain if uncertain else ReadError)(drv.error))
        elif act.msg is not None and self.scheduler is not None:
            # a poll the device did not answer (NAK/timeout): record the attempt
            # and back it off. Without recording it, its timer stays at zero so it
            # is forever the most-overdue item and the scheduler keeps re-handing
            # the same dead register, starving the rest; the backoff then fades a
            # persistently unanswered register out of the rotation entirely.
            self.scheduler.mark_failure(act.msg, time.monotonic())
            self.stats.polls_failed += 1
