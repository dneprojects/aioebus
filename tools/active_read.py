"""Live active-read test (Phase 4b hardware test) -- READS ONLY.

Actively reads a few registers over the enhanced adapter, i.e. aioebus SENDS on
the bus for the first time (arbitration + master request), then decodes the
slave answer. It only ever issues read commands; it never writes a setting.

REQUIRES ebusd stopped: the adapter is single-client, and this uses master
address 0x31 -- the very address ebusd uses, so it is free exactly while ebusd
is down and is known-good on this bus.

    python tools/active_read.py [host] [port]
    (defaults: 192.168.178.179 9999)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from aioebus.active import ActiveReader, ReadError  # noqa: E402
from aioebus.convert import from_dicts  # noqa: E402
from aioebus.messages import MessageDef  # noqa: E402

CFG = Path(__file__).resolve().parent.parent / "configs" / "vaillant"

# a few read-only registers that should answer (by message name); the
# destination address now travels with the config (zz stamped from the filename).
TARGETS = ["FlowTemp", "Statenumber", "WaterPressure", "DisplayedOutsideTemp",
           "ActorFernsteuerung"]


def load_messages() -> list[MessageDef]:
    msgs: list[MessageDef] = []
    for jf in sorted(CFG.glob("*.json")):
        msgs += from_dicts(json.loads(jf.read_text(encoding="utf-8")))
    return msgs


async def run(host: str, port: int) -> None:
    by_name = {m.name: m for m in load_messages()}
    reader, writer = await asyncio.open_connection(host, port)

    async def send(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def recv() -> bytes:
        return await reader.read(4096)

    ar = ActiveReader(send, recv, qq=0x31)
    print(f"active-read on enh:{host}:{port} as master 0x31 (reads only)\n")
    try:
        for name in TARGETS:
            msg = by_name.get(name)
            if msg is None or not (msg.zz and len(msg.zz) == 2):
                print(f"  {name}: not in configs / no address")
                continue
            zz = int(msg.zz, 16)
            try:
                slave = await ar.read_raw(zz, msg.pbsb, msg.msgid, timeout=4.0)
                values = msg.decode_labeled(slave=slave)
                print(f"  {name} @{zz:#04x}: {values}  (slave={slave.hex()})")
            except (ReadError, asyncio.TimeoutError) as exc:
                print(f"  {name} @{zz:#04x}: FAILED ({exc})")
            await asyncio.sleep(0.3)  # pace the bus
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.178.179"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9999
    asyncio.run(run(host, port))
