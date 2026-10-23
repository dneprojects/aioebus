"""Validate the codecs against a live ebusd SQLite telegram log.

Reads the newest slave responses for a few known Vaillant registers and decodes
them with aioebus, so you can eyeball that the lib matches the real bus right now.

Usage:  python tools/validate_against_db.py /path/to/telegrams.db
(Point it at a *copy* of the DB if ebusd is actively writing it.)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from aioebus import decode  # noqa: E402

# (label, zz hex, sb hex, master id dd, type, slice[start:end] of slave hex, divisor)
CHECKS = [
    ("eloBlock FlowTemp", "38", "09", "0d1800", "D2C", (0, 4), None),
    ("eloBlock PartloadHwc", "38", "09", "0da900", "UCH", (0, 2), None),
    ("eloBlock Statenumber", "38", "09", "0dab00", "UCH", (0, 2), None),
    ("eloBlock PowerValue", "38", "09", "0daa00", "HEX:6", (0, 12), None),
    ("VR71 Anlagen-Vorlauf", "26", "23", "06", "D2C", (0, 4), None),
]


def latest(cur, zz, sb, dd):
    row = cur.execute(
        "select s_dd from telegram_hex where zz=? and sb=? and dd=? and s_dd is not null "
        "order by ts desc limit 1",
        (zz, sb, dd),
    ).fetchone()
    return row[0] if row else None


def main(db):
    cur = sqlite3.connect(db)
    print(f"{'register':24} {'slave':16} {'type':6} -> value")
    print("-" * 60)
    for label, zz, sb, dd, typ, (a, b), div in CHECKS:
        s = latest(cur, zz, sb, dd)
        if s is None:
            print(f"{label:24} (not seen)")
            continue
        val = decode(typ, s[a:b], divisor=div)
        print(f"{label:24} {s:16} {typ:6} -> {val}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "telegrams.db")
