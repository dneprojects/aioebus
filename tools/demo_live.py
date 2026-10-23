"""End-to-end demo: load a real ebusd CSV, decode live telegrams from the log.

Parses an ebusd CSV with aioebus, then for every readable eloBlock message pulls
the newest matching telegram out of the SQLite log and decodes it -- entirely in
Python, no ebusd decoding involved.

Usage: python tools/demo_live.py <38.v32.csv> <telegrams.db>  [zz-hex=38]
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from aioebus.messages import parse_csv  # noqa: E402


def main(csv_path, db_path, zz="38"):
    msgs = parse_csv(Path(csv_path).read_text(encoding="utf-8"))
    cur = sqlite3.connect(db_path)
    print(f"{'message':22} {'register':12} {'slave':18} -> decoded")
    print("-" * 78)
    for m in msgs:
        if m.circuit != "elo" or not m.is_read:
            continue
        pb, sb, dd = m.pbsb[:2], m.pbsb[2:4], m.msgid
        row = cur.execute(
            "select s_dd from telegram_hex where zz=? and pb=? and sb=? and dd=? "
            "and s_dd is not null order by ts desc limit 1",
            (zz, pb, sb, dd),
        ).fetchone()
        if not row:
            continue
        try:
            decoded = m.decode_labeled(slave=row[0])
        except Exception as exc:  # noqa: BLE001
            decoded = f"ERR {exc}"
        print(f"{m.name:22} {m.pbsb + ' ' + m.msgid:12} {row[0][:18]:18} -> {decoded}")


if __name__ == "__main__":
    main(*sys.argv[1:])
