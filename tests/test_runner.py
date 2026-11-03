"""EngineRunner: runs the engine on a link and reconnects when the link ends."""
import asyncio

import pytest

from aioebus.engine import EbusEngine
from aioebus.runner import EngineRunner


class _Link:
    """A link that ends (EOF) when told to, or stays open."""

    def __init__(self, ends: bool = True):
        self.ends = asyncio.Event()
        if ends:
            self.ends.set()
        self.closed = False

    async def send(self, data: bytes) -> None:
        pass

    async def recv(self) -> bytes:
        await self.ends.wait()
        raise ConnectionResetError("adapter closed the connection")

    def close(self) -> None:
        self.closed = True


async def _until(cond, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


def test_reconnects_after_each_lost_link():
    async def inner():
        links: list[_Link] = []

        async def open_link():
            links.append(_Link(ends=len(links) < 2))  # the third one stays up
            return links[-1]

        lost = []
        runner = EngineRunner(EbusEngine(), open_link, backoff=0,
                              on_lost=lambda: lost.append(1))
        await runner.start()
        await _until(lambda: len(links) == 3)
        assert len(lost) == 2 and links[0].closed and links[1].closed
        assert not links[2].closed
        runner.close()
        assert links[2].closed and runner.engine.stopped

    asyncio.run(inner())


def test_failed_reconnects_are_retried_after_a_pause():
    async def inner():
        calls = 0

        async def open_link():
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Link()  # ends at once
            if calls < 4:
                raise OSError("adapter unreachable")
            return _Link(ends=False)

        lost = []
        runner = EngineRunner(EbusEngine(), open_link, backoff=0,
                              on_lost=lambda: lost.append(1))
        await runner.start()
        await _until(lambda: calls == 4)
        assert lost == [1]  # one outage, however many attempts it took
        runner.close()

    asyncio.run(inner())


def test_unreachable_adapter_fails_the_start():
    async def open_link():
        raise TimeoutError

    with pytest.raises(TimeoutError):
        asyncio.run(EngineRunner(EbusEngine(), open_link).start())


def test_a_broken_on_lost_callback_does_not_stop_reconnecting():
    async def inner():
        links: list[_Link] = []

        async def open_link():
            links.append(_Link(ends=len(links) < 1))
            return links[-1]

        def broken():
            raise RuntimeError("consumer bug")

        runner = EngineRunner(EbusEngine(), open_link, backoff=0, on_lost=broken)
        await runner.start()
        await _until(lambda: len(links) == 2)
        runner.close()

    asyncio.run(inner())


def test_close_during_the_pause_opens_no_new_link():
    async def inner():
        opened = 0

        async def open_link():
            nonlocal opened
            opened += 1
            return _Link()

        runner = EngineRunner(EbusEngine(), open_link, backoff=0.2)
        await runner.start()
        await asyncio.sleep(0.05)  # the link ended, the runner waits
        runner.close()
        await asyncio.sleep(0.3)
        assert opened == 1

    asyncio.run(inner())
