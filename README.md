# aioebus

A **standalone, async, pure-Python eBUS library**: it talks to an eBUS adapter
(enhanced protocol over TCP) and decodes Vaillant/eBUS messages **without an
ebusd daemon**. The [ebus Home Assistant integration](https://github.com/dneprojects/ebus)
is built on it and runs it in the field.

It ports [ebusd](https://github.com/john30/ebusd)'s behaviour to Python by spec
(not line by line) and checks every layer against real captured telegrams.

## What it does

| Module | |
|---|---|
| `link` | TCP connection to the adapter (`connect`), liveness `probe` |
| `engine` | One connection: decodes what other masters put on the bus (free), slips its own reads/writes in when the bus is idle, keeps a value cache, polls by priority (`poll`) |
| `discovery` | Which devices are on the bus: scan, Vaillant identification (article, serial), deep scan of every slave address, phantom addresses |
| `catalog` | Each device's definitions: our bundled ones, else a user file, else the matching ebusd-configuration CSV from ebus.github.io (chosen as ebusd chooses it, with its `!load` includes), cached; display texts per language |
| `values` | What a message's values are: labels, stable keys, cache keys, numeric or not, the control a write allows |
| `messages`, `codecs` | Message model, ebusd-CSV ingest (default rows, conditions, value tables), every ebusd datatype |
| `frame`, `passive`, `transport`, `active` | Telegrams and CRC, matching telegrams to definitions, the adapter's byte protocol, the transaction state machine |

## Example

```python
import asyncio

from aioebus import Catalog, EbusEngine, MessageRegistry, PollScheduler, connect, discover


async def main() -> None:
    link = await connect("192.168.1.50", 9999)
    engine = EbusEngine(link.send, link.recv, MessageRegistry([]))
    run = asyncio.create_task(engine.run())

    found = await discover(engine)                    # which devices answer
    catalog = Catalog()                               # bundled definitions only
    await catalog.load({a: d.scan for a, d in found.devices.items()},
                       {a: d.ident for a, d in found.devices.items() if d.ident})
    engine.registry = MessageRegistry(catalog.all_messages())
    engine.scheduler = PollScheduler(catalog.device_messages("r"), base_interval=60)

    await asyncio.sleep(120)
    print(engine.cache)                               # (device, message, label) -> value
    engine.stop()
    link.close()
    await run


asyncio.run(main())
```

To load ebusd-configuration CSVs for devices without a bundled definition, give
the catalog a cache directory and a `fetch(url) -> str | None` coroutine
(`""` when the server says the file does not exist, `None` when unreachable).

Writing is opt-in (`EbusEngine(..., allow_write=True)`). A write that went out
completely but was not acknowledged raises `WriteUncertain` and is never sent
twice; read the value back instead.

Decoding a captured telegram needs no bus:

```python
from aioebus import MessageRegistry, decode_raw, parse_csv

reg = MessageRegistry(parse_csv(open("38.v32.csv").read()))
decode_raw(reg, "3138b509030d1800740003910100bb00")   # (FlowTemp def, {'FlowTemp': 25.0625})
```

## Definitions and licensing

The library ships only its own Vaillant definitions (`aioebus/defs/vaillant`,
Apache-2.0, provenance in its `PROVENANCE.md`): re-engineered from observed
telegrams or hand-written, facts only. The ebusd-configuration CSVs are GPL-3.0;
the catalog downloads them on the user's own machine and keeps them in the
caller's cache directory, never in this package. `python -m aioebus.convert`
turns an ebusd CSV into the native JSON format, for local use.

## Dev

```bash
python -m pytest            # tests, incl. real captures
ruff check src tests tools
mypy
python tools/validate_frame_db.py /path/to/telegrams.db   # frames vs ebusd
```
