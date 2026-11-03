"""Keep an engine on the bus: run it on an adapter link and reconnect when the
link ends (adapter rebooted, Wi-Fi dropped, a silent half-open connection).

The engine keeps its value cache and poll scheduler across reconnects; only the
link is replaced (:meth:`EbusEngine.attach`).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from .engine import EbusEngine
from .link import Link

_LOGGER = logging.getLogger(__name__)

RECONNECT_BACKOFF: Final = 10.0  # s between reconnect attempts


class EngineRunner:
    """Runs one engine on links from ``open_link`` (e.g. ``lambda: connect(host,
    port)``) until :meth:`close`. ``on_lost`` is called each time the link ends."""

    def __init__(self, engine: EbusEngine, open_link: Callable[[], Awaitable[Link]], *,
                 backoff: float = RECONNECT_BACKOFF,
                 on_lost: Callable[[], None] | None = None) -> None:
        self.engine = engine
        self._open_link = open_link
        self.backoff = backoff
        self._on_lost = on_lost
        self._link: Link | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Open the first link and start the engine on it; OSError/TimeoutError
        when the adapter cannot be reached. From then on the runner reconnects
        by itself."""
        self._attach(await self._open_link())
        self._task = asyncio.create_task(self._keep_running(), name="ebus-link")

    def close(self) -> None:
        """Stop the engine and close the link; no reconnect follows."""
        self._closed = True
        self.engine.stop()
        if self._task is not None:
            self._task.cancel()
        self._close_link()

    def _attach(self, link: Link) -> None:
        self._link = link
        self.engine.attach(link.send, link.recv)

    def _close_link(self) -> None:
        if self._link is not None:
            self._link.close()
            self._link = None

    async def _keep_running(self) -> None:
        while not self._closed:
            try:
                await self.engine.run()
            except Exception as err:
                _LOGGER.debug("eBUS engine stopped: %s", err)
            if self._closed or self.engine.stopped:
                return
            if self._on_lost is not None:
                with contextlib.suppress(Exception):  # a consumer's bug must not end it
                    self._on_lost()
            self._close_link()
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Try again after each pause until a link opens (close() cancels)."""
        while True:
            await asyncio.sleep(self.backoff)
            try:
                link = await self._open_link()
            except (OSError, TimeoutError) as err:
                _LOGGER.debug("eBUS: reconnect failed: %s", err)
                continue
            self._attach(link)
            _LOGGER.debug("eBUS: reconnected")
            return
