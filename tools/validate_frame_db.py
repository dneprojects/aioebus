"""Conformance: parse every raw telegram in an ebusd SQLite log with
aioebus.frame and check it against ebusd's own decoded columns + CRC.

    python tools/validate_frame_db.py /path/to/telegrams.db
"""
import sqlite3
import sys

sys.path.insert(0, "src")
from aioebus.frame import FrameError, parse_telegram  # noqa: E402


def main(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    # Gate on complete, accepted telegrams; timeouts/invalids are bus noise and
    # single-byte fragments, not frames aioebus is expected to parse.
    rows = con.execute(
        "select type,qq,zz,pb,sb,nn,dd,crc,s_dd,s_crc,raw from telegram "
        "where raw is not null and status='ok'"
    )
    total = parse_fail = mismatch = crc_fail = 0
    examples: list[str] = []
    for typ, qq, zz, pb, sb, nn, dd, crc, s_dd, s_crc, raw in rows:
        total += 1
        try:
            tg = parse_telegram(raw)
        except FrameError as exc:
            parse_fail += 1
            if len(examples) < 10:
                examples.append(f"PARSE {raw}: {exc}")
            continue
        got_dd = tg.data.hex()
        got_sdd = tg.slave_data.hex()
        want_dd = (dd or "").lower()
        want_sdd = (s_dd or "").lower()
        if (tg.qq, tg.zz, tg.pb, tg.sb, tg.nn) != (qq, zz, pb, sb, nn) \
                or got_dd != want_dd or got_sdd != want_sdd or tg.crc != crc:
            mismatch += 1
            if len(examples) < 10:
                examples.append(
                    f"MISMATCH {raw}: got dd={got_dd} sdd={got_sdd} "
                    f"crc={tg.crc} vs db dd={want_dd} sdd={want_sdd} crc={crc}"
                )
            continue
        if not tg.crc_ok:
            crc_fail += 1
            if len(examples) < 10:
                examples.append(f"CRC {raw}: computed != carried")
    ok = total - parse_fail - mismatch - crc_fail
    print(f"telegrams: {total}")
    print(f"  parsed+matched+crc-ok: {ok} ({100 * ok / total:.2f}%)")
    print(f"  parse failures: {parse_fail}")
    print(f"  column mismatches: {mismatch}")
    print(f"  crc failures: {crc_fail}")
    for e in examples:
        print("  !", e)
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
