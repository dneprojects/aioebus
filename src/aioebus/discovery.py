"""Device discovery: which devices are on the bus, and what they are.

Runs through a running :class:`~aioebus.engine.EbusEngine` (one transaction
policy for setup and runtime). The caller keeps what must outlive a restart --
the extra addresses a deep scan found and the identifications read so far -- and
hands it back in; discovery itself stores nothing.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .active import ReadError
from .engine import EbusEngine
from .scan import VAILLANT, Identification, ScanResult

_LOGGER = logging.getLogger(__name__)

# device addresses where the usual devices of a Vaillant system answer: heat
# generator, hot-water unit, controller, a second heat pump behind a coupler,
# mixer/solar module, eloBlock, ventilation, gateway
BASE_ADDRESSES: Final[tuple[int, ...]] = (0x08, 0x0A, 0x15, 0x18, 0x26, 0x38, 0x76, 0xF6)
# the whole device address space a deep scan probes once: the high/low nibble
# families eBUS slaves use (covers every base address)
ALL_ADDRESSES: Final[tuple[int, ...]] = tuple(
    (hi << 4) | lo
    for hi in (0x0, 0x1, 0x2, 0x3, 0x5, 0x7, 0xF)
    for lo in (0x3, 0x4, 0x5, 0x6, 0x8, 0xA, 0xC)
)
BROADCAST_ADDRESS: Final = 0xFE  # telegrams to all; not a device that answers a scan

TRAFFIC_WAIT: Final = 10.0  # s for the first bus symbol before scanning
PROBE_TIMEOUT: Final = 6.0  # s per address (an absent device fails at once: SYN)


class BusSilent(ConnectionError):
    """The adapter is reachable, but no eBUS symbol arrives (no bus power, e.g.
    the heating is switched off, or a half-open link). Try again later."""


@dataclass
class Device:
    """One device on the bus: its address, scan answer and, for a Vaillant
    device that gave it, the extended identification (article, serial)."""
    address: int
    scan: ScanResult
    ident: Identification | None = None


@dataclass
class Discovery:
    """What a discovery found, plus what the caller should keep for the next one:
    ``extra_addresses`` beyond the base set, and ``remembered`` identifications
    (JSON-ready, keyed by two-digit hex address)."""
    devices: dict[int, Device]
    extra_addresses: list[int]
    remembered: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped: set[int] = field(default_factory=set)  # duplicate addresses removed


def remember(device: Device) -> dict[str, Any]:
    """The JSON-ready record of a device's identification (see :func:`recall`)."""
    scan, ident = device.scan, device.ident
    assert ident is not None
    return {"ident": scan.ident, "hw": scan.hw, "sw": scan.sw, "article": ident.article,
            "serial": ident.serial, "year": ident.year, "week": ident.week}


def recall(scan: ScanResult, record: object) -> Identification | None:
    """A remembered identification, if it belongs to this very unit (same ident,
    HW and SW): a replaced unit of the same model must not inherit the serial."""
    if not (isinstance(record, dict) and record.get("ident") == scan.ident
            and record.get("hw") == scan.hw and record.get("sw") == scan.sw):
        return None
    return Identification(record.get("article", ""), record.get("serial", ""),
                          record.get("year"), record.get("week"))


async def discover(
    engine: EbusEngine,
    *,
    extra_addresses: Iterable[int] = (),
    deep: bool = False,
    remembered: Mapping[str, object] | None = None,
) -> Discovery:
    """Scan the bus and identify its devices.

    Probes the base addresses plus ``extra_addresses`` (or, with ``deep``, the
    whole address space once), reads the Vaillant identification, and drops
    phantom addresses (one unit answering on several, recognised by its serial).
    An identification a device does not give now is taken from ``remembered``
    when it belongs to the same unit.

    Raises :class:`BusSilent` when the bus stays silent, ConnectionError when
    the engine stops (the link dropped) -- both mean: try again later."""
    if not await engine.wait_for_traffic(TRAFFIC_WAIT):
        _check_running(engine)  # no traffic because the link dropped
        raise BusSilent("no eBUS traffic")
    stored = dict(remembered or {})
    # a stored address may come from a corrupt or hand-edited store: only real ones
    extras = {a for a in extra_addresses
              if isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= 0xFF}
    targets = sorted(set(ALL_ADDRESSES) | extras) if deep else sorted(set(BASE_ADDRESSES) | extras)
    devices: dict[int, Device] = {}
    for zz in targets:
        _check_running(engine)
        try:
            devices[zz] = Device(zz, await engine.scan(zz, PROBE_TIMEOUT))
        except (ReadError, TimeoutError) as err:
            _check_running(engine)  # a dropped link is no missing device
            _LOGGER.debug("scan %#04x: %s", zz, err)
    out = dict(stored)
    for zz, device in devices.items():
        if device.scan.mf != VAILLANT:
            continue
        _check_running(engine)
        device.ident = await engine.read_identification(zz, PROBE_TIMEOUT)
        key = f"{zz:02x}"
        if device.ident is not None:
            out[key] = remember(device)
        elif (known := recall(device.scan, stored.get(key))) is not None:
            device.ident = known
            _LOGGER.debug("ident %#04x: using the remembered identification", zz)
    _check_running(engine)  # an identification lost to a dropped link: try again later
    dropped = drop_duplicates(devices)
    # remember devices found beyond the base, so later setups keep scanning them; a
    # normal scan keeps one that missed this start, a deep scan (which probed
    # every address) keeps exactly what answered
    found = set(devices) - set(BASE_ADDRESSES)
    keep = sorted((found if deep else extras | found) - dropped)
    return Discovery(devices, keep, out, dropped)


def drop_duplicates(devices: dict[int, Device]) -> set[int]:
    """Drop phantom addresses: a unit answering on several addresses with the same
    serial is one device. Keep a base address (else the lowest); return the rest."""
    primary: dict[str, int] = {}
    dropped: set[int] = set()
    for zz in sorted(devices):
        ident = devices[zz].ident
        if ident is None or not ident.serial:
            continue
        prev = primary.get(ident.serial)
        if prev is None:
            primary[ident.serial] = zz
            continue
        drop = zz if (prev in BASE_ADDRESSES or zz not in BASE_ADDRESSES) else prev
        if drop == prev:
            primary[ident.serial] = zz
        del devices[drop]
        dropped.add(drop)
        _LOGGER.debug("dropped duplicate %#04x (serial %s)", drop, ident.serial)
    return dropped


async def identify_later(
    engine: EbusEngine, devices: Mapping[int, Device], delays: Iterable[float]
) -> AsyncIterator[Device]:
    """Read the identification of Vaillant devices that did not give it at
    discovery (e.g. behind a slow bus coupler), once after each pause in
    ``delays``; yields each device as soon as it is identified."""
    for delay in delays:
        missing = [d for d in devices.values() if d.scan.mf == VAILLANT and d.ident is None]
        if not missing:
            return
        await asyncio.sleep(delay)
        for device in missing:
            try:
                ident = await engine.read_identification(device.address)
            except (TimeoutError, ReadError, OSError) as err:
                _LOGGER.debug("ident %#04x later: %s", device.address, err)
                continue
            if ident is not None:
                device.ident = ident
                yield device


def _check_running(engine: EbusEngine) -> None:
    if not engine.running:
        raise ConnectionError("the eBUS link dropped during discovery")
