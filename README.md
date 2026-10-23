# aioebus

A **standalone, async, pure-Python eBUS library** — the goal is to talk to an
eBUS adapter (enhanced protocol) and decode Vaillant/eBUS messages **without a
separate ebusd daemon**, so a Home Assistant integration can embed it directly
(lib in PyPI, thin integration in HA Core).

This is an incremental port of [ebusd](https://github.com/john30/ebusd)'s
behaviour to Python — **not** a line-by-line transpile. Each layer is ported by
spec and checked against real captured telegrams (a conformance harness), so the
long tail of real-world quirks is caught automatically.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold + conformance harness (real captures as golden data) | ✅ |
| 1 | **Datatype codecs** (`datatype.cpp` port) + CSV type specs | ✅ |
| 2 | **Message model + CSV ingest + decode** (default rows, IGN, value tables) | ✅ |
| 2.5 | **Full datatypes** (date/time/float/bit), **conditions** as metadata | ✅ |
| 3 | **eBUS frame/CRC + passive decode** (wire bytes → named values) | ✅ |
| 3b | **Enhanced-adapter transport** (protocol ✅; live socket needs a window) | ◐ |
| 4 | Active read: build + scheduler + **send FSM ✅ (offline)**; live send, scan, write | ◐ |
| 5 | Thin HA integration swaps its ebusd-HTTP client for this lib | ⏳ |

The enhanced adapter does bus arbitration in hardware, so the timing-critical
part is offloaded — Phase 3 ports a documented host↔adapter byte protocol, not
microsecond timing.

## Phase 1 — what works now

Pure, offline decoding of eBUS field types, validated against telegrams captured
from a real Vaillant cascade (2× aroTHERM, sensoCOMFORT, eloBlock, VR 71):

```python
from aioebus import decode

decode("D2C", "bd01")            # 27.8125  (flow temp)
decode("UCH", "0f")             # 15
decode("STR:5", "4e45545833")   # "NETX3"
decode("HEX:6", "090264186418") # "09 02 64 18 64 18"
decode("SIN", "f401", divisor=10)  # 50.0  (CSV divisor column)
decode("UCH", "ff")             # None  (invalid/replacement sentinel)
```

Supported types: the integers (UCH SCH UIN SIN ULG U2L U4L, reversed UIR SIR),
fixed-point D1B D1C D2B D2C FLT, floats EXP/EXR, BCD[:n] PIN, HEX:n, STR:n/STR:*,
IGN:n, bit fields BI0..BI7[:n], and the date/time family (TTM TTH TTQ, HTM BTM
VTM, HTI BTI VTI, HDA BDA[:3], BDY HDY, DTM DAY) -- with the CSV divisor column
and ebusd's replacement (invalid) handling. This resolves every field type used
across the 104 stock Vaillant configs.

## Phase 2 — structured messages + CSV ingest

The in-memory model is structured dataclasses (`MessageDef` / `FieldDef`), never
CSV-shaped. The community ebusd-CSV library is read *into* the model with Python's
`csv` module (so quoted commas no longer abort a file). Our own curated
definitions can instead live in the native JSON format (`convert.py`) — both
become the same objects.

```python
from aioebus.messages import parse_csv

msgs = {m.name: m for m in parse_csv(open("38.v32.csv").read())}
msgs["Statenumber"].decode_labeled(slave="1f")   # {'Statenumber': 'sommer'}
msgs["PositionValveSet"].decode_labeled(slave="64")  # {'PositionValveSet': 'dhw'}
```

`tools/demo_live.py <csv> <telegrams.db>` parses a real config and decodes the
newest live telegram for every message — pure Python, no ebusd decoding.
`python -m aioebus.convert <csv> [out.json]` converts curated CSVs to the native
structured format.

## Phase 3 — frame layer + passive decode

The wire format is parsed end to end: escaping (`A9 01`→`AA`, `A9 00`→`A9`), the
eBUS CRC-8 (polynomial `0x9B`, computed over the *escaped* stream), and the full
frame structure incl. NAK-retransmission. Combined with the message model this
turns raw bytes seen on the bus into named values, read-only:

```python
from aioebus import parse_csv, MessageRegistry, decode_raw

reg = MessageRegistry(parse_csv(open("38.v32.csv").read()))
decode_raw(reg, "3138b509030d1800740003910100bb00")   # (FlowTemp def, {'FlowTemp': 25.0625})
```

Validated bit-exact against **285 358** complete telegrams captured from the live
bus: every one parses, matches ebusd's own decoded columns and verifies CRC
(`tools/validate_frame_db.py`).

## Phase 3b — enhanced-adapter transport

`transport.py` decodes the adapter's framed byte stream (`11ccccdd 10dddddd`
symbols, short form for bytes < 0x80), assembles telegrams on SYN and decodes
them — all read-only, nothing is ever written to the bus:

```python
from aioebus import parse_csv, MessageRegistry, listen

reg = MessageRegistry(parse_csv(open("38.v32.csv").read()))
async for msgdef, values in listen("192.168.178.179", 9999, reg):
    print(values)
```

The protocol logic is validated offline by round-tripping real telegrams through
a synthetic adapter stream, including arbitrary read fragmentation
(`tests/test_transport.py`). Live listening is gated on hardware: the enhanced
adapter is single-client, so ebusd must be stopped first (a maintenance window).
It has been run live against the reference adapter — passive decode works
end-to-end without ebusd.

## Phase 4 — active reads under limited bandwidth

`active.build_read()` constructs the on-wire master request; its CRC matches
ebusd for every real read in the log. `poll.PollScheduler` decides *what* to read
and *when*, because eBUS is ~2400 baud and only a handful of reads fit per second:

- only `r1`..`r9` are polled; the priority digit scales the interval (`r1` most
  frequent), plain `r` is on-demand, `w`/`u` never;
- **passive-first** — a value seen on the bus refreshes that register's timer, so
  broadcasts are never re-polled (on the live bus this is most of the traffic);
- one request at a time — the scheduler hands back only the most-overdue message.

The bus *send* FSM (`active.ReadDriver`/`ActiveReader`) is ported from ebusd's
`protocol_direct.cpp`: arbitrate (START with QQ) → send `ZZ PB SB NN DD` + CRC,
each byte echoed back and **verified for collision** → master ACK → read the
slave frame and check its CRC → ACK → SYN. The pure `ReadDriver` is driven in
tests by a simulated adapter through a full round-trip (arbitration-won, echoes,
answer, decode) plus the arbitration-lost and collision paths — all offline.

The live send (`tools/active_read.py`, reads only) uses master address **0x31**,
the address ebusd itself uses on the reference bus: known-good, and free exactly
while ebusd is stopped (which the single-client adapter requires anyway). It has
not been run on hardware yet — that is the next maintenance window.

## Native config format + converter

`configs/vaillant/*.json` are native-format device definitions (eloBlock, netx3)
— **our own, independently-created** definitions, Apache-2.0. `aioebus.convert`
turns any ebusd CSV into this format:

```bash
python -m aioebus.convert path/to/15.ctlv3.csv ctlv3.json
```

The ebusd-configuration CSVs are GPL-3.0, so a file converted from them is a
GPL-3.0 derivative: convert those **locally, against your own copy** — this
Apache-2.0 library ships only our own definitions, never a derivative of that
GPL data. `tools/convert_configs.py` regenerates our own set and the HA
translations base.

Names are **not** baked in: `aioebus.translations.ha_strings()` derives a stable
`translation_key` per field and emits `configs/vaillant/translations/strings.json`
(the English base); a `de.json` is a translated copy with the same keys.

## Dev

```bash
python -m pytest            # conformance tests (real captures)
python tools/validate_frame_db.py /path/to/telegrams.db   # frames vs ebusd (100%)
python tools/validate_against_db.py /path/to/telegrams.db  # optional field decode
```
