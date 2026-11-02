"""Device discovery against a fake engine (scan/identification answers by address)."""
import asyncio

import pytest

from aioebus.active import ReadError
from aioebus.discovery import (
    ALL_ADDRESSES,
    BASE_ADDRESSES,
    BusSilent,
    Device,
    discover,
    identify_later,
)
from aioebus.scan import Identification, ScanResult

HMU = ScanResult(0xB5, "Vaillant", "HMU00", "0902", "5103", 902, 5103)
CTL = ScanResult(0xB5, "Vaillant", "CTLV3", "0808", "8004", 808, 8004)
OTHER = ScanResult(0x19, "0x19", "XYZ", "0100", "0200", 100, 200)
SERIAL = "21212300202609130000000000N1"


def _ident(serial=SERIAL):
    return Identification("0020260913", serial, 2021, 23)


class FakeEngine:
    def __init__(self, scans, idents=None, *, traffic=True, drop_after=None):
        self.scans, self.idents = scans, idents or {}
        self.traffic, self.drop_after = traffic, drop_after
        self.running = True
        self.probed: list[int] = []

    async def wait_for_traffic(self, timeout):
        return self.traffic

    async def scan(self, zz, timeout=6.0):
        self.probed.append(zz)
        if self.drop_after is not None and len(self.probed) > self.drop_after:
            self.running = False
            raise ReadError("connection lost")
        if zz not in self.scans:
            raise ReadError("no answer from slave")
        return self.scans[zz]

    async def read_identification(self, zz, timeout=6.0):
        return self.idents.get(zz)


def run(coro):
    return asyncio.run(coro)


def test_silent_bus_raises_before_any_probe():
    eng = FakeEngine({0x08: HMU}, traffic=False)
    with pytest.raises(BusSilent):
        run(discover(eng))
    assert eng.probed == []


def test_base_addresses_and_extras_are_probed_and_identified():
    eng = FakeEngine({0x08: HMU, 0x15: CTL, 0x52: OTHER}, {0x08: _ident()})
    found = run(discover(eng, extra_addresses=[0x52]))
    assert eng.probed == sorted({*BASE_ADDRESSES, 0x52})
    assert set(found.devices) == {0x08, 0x15, 0x52}
    assert found.devices[0x08].ident == _ident()
    assert found.devices[0x52].ident is None  # not Vaillant: no identification read
    assert found.extra_addresses == [0x52]
    assert found.remembered["08"]["serial"] == SERIAL


def test_deep_scan_probes_everything_and_keeps_exactly_what_answered():
    eng = FakeEngine({0x08: HMU, 0x53: OTHER})
    found = run(discover(eng, extra_addresses=[0x52], deep=True))
    assert set(eng.probed) == set(ALL_ADDRESSES) | {0x52}
    assert found.extra_addresses == [0x53]  # 0x52 answered nothing: forgotten


def test_normal_scan_keeps_an_extra_that_missed_one_start():
    found = run(discover(FakeEngine({0x08: HMU}), extra_addresses=[0x52]))
    assert found.extra_addresses == [0x52]


def test_remembered_identification_only_for_the_same_unit():
    record = {"ident": "HMU00", "hw": "5103", "sw": "0902", "article": "0020260913",
              "serial": SERIAL, "year": 2021, "week": 23}
    found = run(discover(FakeEngine({0x08: HMU}), remembered={"08": record}))
    assert found.devices[0x08].ident == _ident()
    replaced = ScanResult(0xB5, "Vaillant", "HMU00", "0903", "5103", 903, 5103)
    found = run(discover(FakeEngine({0x08: replaced}), remembered={"08": record}))
    assert found.devices[0x08].ident is None  # another unit: no inherited serial
    assert found.remembered == {"08": record}  # kept for its unit


def test_one_unit_on_two_addresses_keeps_the_base_one():
    eng = FakeEngine({0x18: HMU, 0x52: HMU}, {0x18: _ident(), 0x52: _ident()})
    found = run(discover(eng, extra_addresses=[0x52]))
    assert set(found.devices) == {0x18} and found.dropped == {0x52}
    assert found.extra_addresses == []  # the phantom is not remembered


def test_link_dropping_during_the_scan_fails_the_discovery():
    eng = FakeEngine({0x08: HMU, 0x15: CTL}, drop_after=2)
    with pytest.raises(ConnectionError):
        run(discover(eng))
    assert len(eng.probed) == 3  # stopped at once, not probing every address


def test_identification_read_later():
    eng = FakeEngine({}, {0x18: _ident()})
    devices = {0x18: Device(0x18, HMU), 0x15: Device(0x15, OTHER)}

    async def collect():
        return [d async for d in identify_later(eng, devices, (0, 0))]

    later = run(collect())
    assert [d.address for d in later] == [0x18] and devices[0x18].ident == _ident()


def test_deep_scan_space_covers_the_base_addresses():
    assert set(BASE_ADDRESSES) <= set(ALL_ADDRESSES)
    assert len(ALL_ADDRESSES) == 49  # 7 high x 7 low nibble families


def test_traffic_wait_ends_when_the_link_drops():
    from aioebus.engine import EbusEngine
    from aioebus.passive import MessageRegistry

    async def send(data):
        pass

    async def recv():
        raise ConnectionResetError("closed")

    async def inner():
        eng = EbusEngine(send, recv, MessageRegistry([]))
        task = asyncio.create_task(eng.run())
        assert not await asyncio.wait_for(eng.wait_for_traffic(5), 1)  # not after 5 s
        await task
        with pytest.raises(ConnectionError):  # a dropped link, not a silent bus
            await discover(eng)

    run(inner())


def test_link_dropping_during_identification_fails_the_discovery():
    class DropsOnIdent(FakeEngine):
        async def read_identification(self, zz, timeout=6.0):
            self.running = False  # the link went while reading it
            return None

    with pytest.raises(ConnectionError):
        run(discover(DropsOnIdent({0x08: HMU})))


def test_invalid_stored_addresses_are_ignored():
    eng = FakeEngine({0x08: HMU})
    found = run(discover(eng, extra_addresses=[300, -1, True, "52", 0x52]))
    assert set(eng.probed) == {*BASE_ADDRESSES, 0x52}
    assert found.extra_addresses == [0x52]


def test_reattached_engine_waits_for_its_new_run():
    from aioebus.engine import EbusEngine
    from aioebus.passive import MessageRegistry

    async def send(data):
        pass

    async def gone():
        raise ConnectionResetError("closed")

    async def inner():
        eng = EbusEngine(send, gone, MessageRegistry([]))
        await eng.run()  # the first link ends
        queue: asyncio.Queue = asyncio.Queue()
        eng.attach(send, queue.get)  # reconnected; the new run() starts a bit later
        waiting = asyncio.create_task(eng.wait_for_traffic(1))
        await asyncio.sleep(0.05)
        assert not waiting.done()  # not ended at once by the previous run
        task = asyncio.create_task(eng.run())
        queue.put_nowait(bytes((0xC6, 0xAA)))  # a SYN symbol
        assert await waiting
        eng.stop()
        queue.put_nowait(b"")
        await task

    run(inner())
