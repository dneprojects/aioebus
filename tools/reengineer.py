"""Re-engineer own device definitions from observation.

Takes an ebusd-configuration device CSV purely as a *decoding key* (the message
ids, datatypes, divisors and units are facts about the device) and keeps only
the registers that actually carry a value on *this* installation's bus, decided
against a captured telegram log (the ebusd SQLite `telegram` table). The result
is our own selection -- the subset observed here, not upstream's full curated
compilation -- emitted in the native JSON format.

    python tools/reengineer.py <telegrams.db> <out_dir> \
        [--device ADDR NAME.json CSV [CSV ...]] ...

Example (one --device group per emitted file):

    python tools/reengineer.py log.db out \
        --device 15 ctlv3.json csv/15.ctlv3.csv \
        --device 08 hmu.json  csv/08.hmu.csv csv/08.hmu.HW5103.csv

A message is kept when at least one of its fields decoded to a non-None value
somewhere in the log; every field of a kept message is retained so byte offsets
stay correct. Only facts (ids/types/divisors/units/value tables) and the short
functional field names are carried over.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aioebus.convert import to_dicts
from aioebus.messages import MessageDef, parse_csv

BROADCAST = 0xFE


def _load_defs(csv_paths: list[Path]) -> list[MessageDef]:
    """Parse and concatenate one device's CSVs, dropping identical
    (direction, pbsb, id, name) rows a HW/circuit overlay repeats."""
    seen: set[tuple] = set()
    defs: list[MessageDef] = []
    for p in csv_paths:
        for m in parse_csv(p.read_text(encoding="utf-8", errors="replace")):
            key = (m.direction, m.pbsb, m.msgid, m.name)
            if key not in seen:
                seen.add(key)
                defs.append(m)
    return defs


def _by_pbsb(defs: list[MessageDef]) -> dict[tuple[int, int], list[MessageDef]]:
    idx: dict[tuple[int, int], list[MessageDef]] = {}
    for m in defs:
        if len(m.pbsb) == 4:
            idx.setdefault((int(m.pbsb[:2], 16), int(m.pbsb[2:], 16)), []).append(m)
    return idx


def _match(dd: str, cands: list[MessageDef]) -> list[MessageDef]:
    """Defs whose message id prefixes this master data; keep the longest-id
    group so a short-id sibling does not claim a longer id's telegrams."""
    dd = dd.lower()
    hits = [m for m in cands if m.msgid and dd.startswith(m.msgid.lower())]
    if not hits:
        return []
    longest = max(len(m.msgid) for m in hits)
    return [m for m in hits if len(m.msgid) == longest]


def _live_fields(m: MessageDef, dd: str, s_dd: str) -> set[str]:
    """Field names of m decoding to a non-None value for this telegram."""
    try:
        out = m.decode(master=dd[len(m.msgid):], slave=s_dd)
    except Exception:
        return set()
    return {k for k, v in out.items() if v is not None}


def reengineer(db_path: Path, addr: int | None, csv_paths: list[Path]):
    """Return (kept_defs, report) for one device address, or for the broadcast
    pseudo-circuit when addr is None (telegrams sent to 0xfe)."""
    defs = _load_defs(csv_paths)
    idx = _by_pbsb(defs)
    live: dict[int, set[str]] = {id(m): set() for m in defs}
    seen: dict[int, int] = {id(m): 0 for m in defs}

    db = sqlite3.connect(db_path)
    try:
        if addr is None:
            rows = db.execute(
                "SELECT pb, sb, dd, s_dd FROM telegram WHERE status='ok' AND zz=?",
                (BROADCAST,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT pb, sb, dd, s_dd FROM telegram "
                "WHERE status='ok' AND (zz=? OR (qq=? AND zz=?))",
                (addr, addr, BROADCAST),
            ).fetchall()
    finally:
        db.close()

    for pb, sb, dd, s_dd in rows:
        cands = idx.get((pb, sb))
        if not cands:
            continue
        for m in _match(dd or "", cands):
            seen[id(m)] += 1
            live[id(m)] |= _live_fields(m, dd or "", s_dd or "")

    # A read register is live when any of its fields decoded a value. A write
    # register carries no value passively (writes don't happen on their own),
    # so it is kept when its read twin -- same circuit+name -- is live; that is
    # the current setting the number/select entity shows. Pure write commands
    # with no observable value are dropped.
    live_read_keys = {
        (m.circuit, m.name) for m in defs if m.is_read and live[id(m)]
    }

    def keep(m: MessageDef) -> bool:
        return bool(live[id(m)]) or (m.is_write and (m.circuit, m.name) in live_read_keys)

    kept = [m for m in defs if keep(m)]
    report = {
        "total": len(defs),
        "kept": len(kept),
        "kept_read": sum(1 for m in kept if m.is_read),
        "kept_write": sum(1 for m in kept if m.is_write),
        "dropped": [
            f"{m.direction} {m.pbsb}:{m.msgid} {m.name}"
            + ("" if seen[id(m)] else " (never seen)")
            for m in defs
            if not keep(m)
        ],
        # read fields present in a kept read message but never carrying a value
        # (retained for byte-offset integrity); writes excluded -- their value
        # lives in the read twin, so their fields are never "live" here
        "dead_fields_in_kept": [
            f"{m.name}.{f.name}"
            for m in kept
            if m.is_read
            for f in m.fields
            if f.name and not f.type.upper().startswith("IGN") and f.name not in live[id(m)]
        ],
    }
    return kept, report


def _parse_groups(argv: list[str]) -> list[tuple[int | None, str, list[Path]]]:
    """--device ADDR NAME CSV... or --broadcast NAME CSV... (addr None)."""
    groups: list[tuple[int | None, str, list[Path]]] = []
    i = 0
    while i < len(argv):
        kind = argv[i]
        if kind == "--device":
            addr: int | None = int(argv[i + 1], 16)
            name, i = argv[i + 2], i + 3
        elif kind == "--broadcast":
            addr, name, i = None, argv[i + 1], i + 2
        else:
            raise SystemExit(f"expected --device/--broadcast, got {kind!r}")
        paths = []
        while i < len(argv) and argv[i] not in ("--device", "--broadcast"):
            paths.append(Path(argv[i]))
            i += 1
        groups.append((addr, name, paths))
    return groups


def main(argv: list[str]) -> None:
    db_path, out_dir = Path(argv[0]), Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for addr, name, csv_paths in _parse_groups(argv[2:]):
        kept, report = reengineer(db_path, addr, csv_paths)
        zz = "fe" if addr is None else f"{addr:02x}"
        stamped = [replace(m, zz=zz) for m in kept]
        if kept:
            (out_dir / name).write_text(
                json.dumps(to_dicts(stamped), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        label = "broadcast" if addr is None else f"0x{addr:02x}"
        print(f"\n=== {name} ({label}) ===")
        if not kept:
            print("  nothing kept -- no file written")
        print(f"  kept {report['kept']}/{report['total']} messages")
        if report["dropped"]:
            print(f"  dropped {len(report['dropped'])} value-less messages:")
            for d in report["dropped"]:
                print(f"    - {d}")
        if report["dead_fields_in_kept"]:
            print(f"  note: {len(report['dead_fields_in_kept'])} fields in kept "
                  f"messages carried no value in the log (retained for decode):")
            for d in report["dead_fields_in_kept"]:
                print(f"    . {d}")


if __name__ == "__main__":
    main(sys.argv[1:])
