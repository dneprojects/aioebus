"""Live active-WRITE test (Phase 4c) -- gated and safe by default.

Reads a register's current value, then writes a value back. With no value given
it writes the SAME value (a no-op: proves the write path end to end without
changing any setting). It is a DRY RUN unless you pass --yes, and you choose the
register, so nothing is ever written without an explicit, specific go.

    python tools/active_write.py <MessageName> [value] [--yes] [--host H] [--port P]

Requires ebusd stopped (single-client adapter); writes as master 0x31.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from aioebus.active import ActiveReader, ActiveWriter, ReadError
from aioebus.convert import from_dicts

CFG = Path(__file__).resolve().parent.parent / "configs" / "vaillant"


def load_by_dir(direction_first: str):
    out = {}
    for jf in sorted(CFG.glob("*.json")):
        for m in from_dicts(json.loads(jf.read_text(encoding="utf-8"))):
            if m.direction[:1] == direction_first and m.zz and len(m.zz) == 2:
                out.setdefault(m.name, m)
    return out


async def run(name: str, value: str | None, do_write: bool, host: str, port: int) -> None:
    reads = load_by_dir("r")
    writes = load_by_dir("w")
    rmsg, wmsg = reads.get(name), writes.get(name)
    if wmsg is None:
        print(f"no writable message named {name!r}")
        return
    zz = int(wmsg.zz, 16)
    reader, writer = await asyncio.open_connection(host, port)

    async def send(b):
        writer.write(b)
        await writer.drain()

    async def recv():
        return await reader.read(4096)

    try:
        current = None
        if rmsg is not None:
            try:
                current = (await ActiveReader(send, recv).read(rmsg, timeout=4.0)).get(name)
                print(f"current {name} @{zz:#04x} = {current}")
            except (TimeoutError, ReadError) as exc:
                print(f"could not read current value: {exc}")

        target = value if value is not None else current
        if target is None:
            print("no value to write (give one explicitly)")
            return
        # keep the read value's type for a true no-op; parse an explicit one
        if value is not None:
            try:
                target = int(value)
            except ValueError:
                try:
                    target = float(value)
                except ValueError:
                    pass  # leave as string (enum label)

        print(f"{'WRITING' if do_write else '[dry-run] would write'} "
              f"{name} @{zz:#04x} = {target}")
        if not do_write:
            print("dry run; pass --yes to actually write")
            return

        await ActiveWriter(send, recv, allow_write=True).write(wmsg, {name: target}, timeout=4.0)
        print("write ACKed by the device")
        if rmsg is not None:
            after = (await ActiveReader(send, recv).read(rmsg, timeout=4.0)).get(name)
            print(f"read back {name} = {after}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("value", nargs="?", default=None)
    ap.add_argument("--yes", action="store_true", help="actually write (else dry run)")
    ap.add_argument("--host", default="192.168.178.179")
    ap.add_argument("--port", type=int, default=9999)
    a = ap.parse_args()
    asyncio.run(run(a.name, a.value, a.yes, a.host, a.port))
