"""Replay a captured telegram log through the engine.

A regression check for the bundled device definitions: the recorded ``raw`` wire
bytes of an ebusd SQLite capture go through the same steps as a live bus --
scan answers -> catalog -> message registry -> the engine's passive decoding ->
value cache -- with no adapter touched, so it runs while ebusd keeps the adapter.

    python tools/replay.py <telegrams.db> [--scan 08,0a,15,...] [--values]

The scan addresses default to a typical Vaillant bus; device idents and SW/HW
are read from the capture's own 0704 answers, so the result reflects whatever
installation produced the log. --values also prints the decoded values.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aioebus.catalog import Catalog
from aioebus.discovery import BASE_ADDRESSES
from aioebus.engine import EbusEngine
from aioebus.messages import MessageDef
from aioebus.passive import MessageRegistry
from aioebus.scan import ScanResult, decode_scan
from aioebus.transport import encode_received_frame
from aioebus.values import control_of


def _scan_devices(db: sqlite3.Connection, addrs: tuple[int, ...]) -> dict[int, ScanResult]:
    """Resolve each address from its latest 0704 answer in the capture."""
    devices: dict[int, ScanResult] = {}
    for zz in addrs:
        row = db.execute(
            "SELECT s_dd FROM telegram WHERE status='ok' AND zz=? AND pb=7 AND sb=4 "
            "AND s_dd!='' ORDER BY ts DESC LIMIT 1", (zz,)).fetchone()
        if row:
            devices[zz] = decode_scan(bytes.fromhex(row[0]))
    return devices


async def replay(engine: EbusEngine, frames: Iterator[str]) -> None:
    """Feed recorded frames (hex) to the engine as an adapter stream; a row that
    is no hex is skipped. Ends with the capture."""

    async def recv() -> bytes:
        for raw in frames:
            try:
                return encode_received_frame(bytes.fromhex(raw))
            except ValueError:
                continue
        raise ConnectionResetError("end of capture")

    async def send(_data: bytes) -> None:
        raise OSError("a replay only listens")

    engine.attach(send, recv)
    await engine.run()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Replay a telegram log through the engine.")
    ap.add_argument("db", type=Path, help="path to an ebusd telegrams.db capture")
    ap.add_argument("--scan", help="comma-separated hex addresses to scan (default: Vaillant set)")
    ap.add_argument("--values", action="store_true", help="also print the decoded values")
    args = ap.parse_args(argv)

    addrs = (tuple(int(x, 16) for x in args.scan.split(",")) if args.scan else BASE_ADDRESSES)
    db = sqlite3.connect(args.db)

    devices = _scan_devices(db, addrs)
    catalog = Catalog()  # bundled definitions only: no download; adds the broadcasts
    asyncio.run(catalog.load(devices))
    msgs = catalog.all_messages()
    reads = catalog.device_messages("r")
    writes = [m for m in catalog.device_messages("w") if control_of(m)]

    hits = 0

    def count(_msg: MessageDef, _values: dict) -> None:
        nonlocal hits
        hits += 1

    engine = EbusEngine(registry=MessageRegistry(msgs), on_value=count)
    rows = (raw for (raw,) in db.execute("SELECT raw FROM telegram WHERE status='ok'"))
    asyncio.run(replay(engine, rows))
    cache = {k: v for k, v in engine.cache.items() if v is not None}

    print(f"devices scanned: {len(devices)}  message defs: {len(msgs)}")
    print(f"messages: {len(reads)} read, {len(writes)} with a control")
    print(f"telegrams: {engine.stats.telegrams}, decoded: {hits}; "
          f"distinct values: {len(cache)}\n")

    by_zz: dict[str, int] = defaultdict(int)
    for (zz, _, _) in cache:
        by_zz[zz] += 1
    for zz in sorted(catalog.addresses):
        scan = catalog.scan(zz)
        assert scan is not None
        key = f"{zz:02x}"
        print(f"  {key} {scan.ident:10} {by_zz.get(key, 0):4} live values")

    read_keys = {(m.zz, m.name) for m in reads}
    cached_names = {(zz, nm) for (zz, nm, _) in cache}
    missing = sorted(k for k in read_keys if k not in cached_names)
    print(f"\nread entities with a value: {len(read_keys) - len(missing)}/{len(read_keys)}")
    for zz, nm in missing[:20]:
        print(f"    no value: {zz} {nm}")

    if args.values:
        print("\ndecoded values:")
        for (zz, name, field), val in sorted(cache.items()):
            print(f"  {zz} {name} [{field}] = {val}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
