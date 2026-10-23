"""Convert the device configs of one real installation to the native JSON format
and (re)generate the combined Home Assistant strings base.

This curates only the files ebusd actually loaded for the reference system -- not
the whole upstream library -- so it stays a bounded, regeneratable snapshot:
  * the source CSVs are pinned under configs/vaillant/src/
  * the JSON is a generated artifact next to them
  * translations/strings.json is the English base to copy into de.json

Run (defaults to the pinned sources in the repo):

    python tools/convert_configs.py [SRC_DIR]

With SRC_DIR it reads the CSVs from there (e.g. an ebusd config path) and also
refreshes the pinned copies.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "src")
from aioebus.convert import from_dicts, to_dicts
from aioebus.messages import parse_csv
from aioebus.translations import ha_strings

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "configs" / "vaillant"
SRC = CFG / "src"

# Our own, independently-created definitions only (Apache-2.0). Definitions
# derived from the GPL-3.0 ebusd-configuration are NOT kept here; convert those
# locally with `python -m aioebus.convert <file.csv>` against your own copy.
DEVICES = {
    "eloblock@38": "38.v32.csv",
    "netx@f6": "f6.netx3.csv",
    "arotherm@08": "arotherm.csv",
}


def main(src_dir: str | None) -> int:
    CFG.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    all_msgs = []
    rc = 0
    for label, name in DEVICES.items():
        pinned = SRC / name
        if src_dir:  # refresh the pinned copy from the given source directory
            upstream = Path(src_dir) / name
            if upstream.exists():
                shutil.copyfile(upstream, pinned)
        if not pinned.exists():
            print(f"  ! {label}: missing source {pinned.name}")
            rc = 1
            continue
        text = pinned.read_text(encoding="utf-8", errors="replace")
        msgs = parse_csv(text)
        # ebusd derives the device address from the scanconfig filename, not the
        # CSV; make the JSON self-contained by stamping it onto every message
        # that does not carry its own zz, so active reads know the destination.
        addr = name.split(".")[0].lower()
        if len(addr) == 2 and all(c in "0123456789abcdef" for c in addr):
            for m in msgs:
                if not m.zz:
                    m.zz = addr
        # round-trip check: the full dataclasses survive CSV -> JSON -> model
        back = from_dicts(json.loads(json.dumps(to_dicts(msgs))))
        ok = back == msgs
        (CFG / name.replace(".csv", ".json")).write_text(
            json.dumps(to_dicts(msgs), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        all_msgs += msgs
        cond = sum(1 for m in msgs if m.conditions)
        print(f"  {label:12} {name:22} {len(msgs):4} msgs ({cond} cond)  round-trip={'ok' if ok else 'FAIL'}")
        rc |= 0 if ok else 1

    strings = ha_strings(all_msgs)
    (CFG / "translations").mkdir(exist_ok=True)
    (CFG / "translations" / "strings.json").write_text(
        json.dumps(strings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n  strings.json: {len(strings['entity']['sensor'])} entities "
          f"(copy to de.json and translate the values)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
