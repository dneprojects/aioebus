"""Live passive listen against the enhanced adapter (Phase 3b hardware test).

Connects read-only to the enh adapter, loads every curated device config, and
decodes telegrams off the bus for a few seconds. REQUIRES the adapter to be free
-- stop ebusd first (single-client), run this, then restart ebusd.

    python tools/live_listen.py [host] [port] [seconds]
    (defaults: 192.168.178.179 9999 25)
"""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
from aioebus.convert import from_dicts
from aioebus.messages import MessageDef
from aioebus.passive import MessageRegistry
from aioebus.transport import listen

CFG = Path(__file__).resolve().parent.parent / "configs" / "vaillant"


def load_registry() -> MessageRegistry:
    msgs: list[MessageDef] = []
    for jf in sorted(CFG.glob("*.json")):
        msgs += from_dicts(json.loads(jf.read_text(encoding="utf-8")))
    print(f"loaded {len(msgs)} message defs from {len(list(CFG.glob('*.json')))} configs")
    return MessageRegistry(msgs)


async def run(host: str, port: int, seconds: float) -> None:
    reg = load_registry()
    seen: dict[str, object] = {}
    by_circuit = Counter()
    n = 0
    print(f"listening on enh:{host}:{port} for {seconds:.0f}s (read-only)...")
    try:
        gen = listen(host, port, reg)
        async with asyncio.timeout(seconds):
            async for msgdef, values in gen:
                n += 1
                by_circuit[msgdef.circuit] += 1
                for k, v in values.items():
                    seen[f"{msgdef.circuit}.{k}"] = v
    except TimeoutError:
        pass
    except OSError as exc:
        print(f"connection failed: {exc}")
        return
    print(f"\ndecoded {n} telegrams; per circuit: {dict(by_circuit)}")
    print(f"distinct values seen: {len(seen)}\n")
    for key in sorted(seen)[:60]:
        print(f"  {key:42} {seen[key]}")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.178.179"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9999
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    asyncio.run(run(host, port, secs))
