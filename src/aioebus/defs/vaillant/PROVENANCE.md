# Provenance of the Vaillant device definitions

All definitions in this folder (`aioebus/defs/vaillant/`) are our own Apache-2.0 work. None is a copy of
the GPL-3.0 `ebusd-configuration` database.

Two kinds live here:

- **Authored defs** — `38.v32` (eloBlock) and `f6.netx3`. Hand-written from
  observation of the reference installation, kept as CSV in the aioebus repository
  (`src/aioebus/defs/vaillant/src/`, not part of the package) and converted to JSON with
  `convert.csv_to_json` (`38.v32` became `eloblock.json`).

- **Re-engineered device types** — `ctlv3`, `pmw`, `vr_71`, `hmu` and
  `broadcast`. Produced by aioebus `tools/reengineer.py` from a captured telegram log of
  the reference bus. The tool uses an `ebusd-configuration` device CSV purely as
  a *decoding key* and keeps only the registers that actually carried a value on
  this installation — our own selection of observed facts, not upstream's full
  curated compilation. Retained from the CSV are facts only: message ids,
  datatypes, divisors, units, value tables, and the short functional field
  names. The source CSVs are **not** redistributed here.

Hand-corrected after re-engineering: in `ctlv3.json` the zones' operating mode
reads `0=off;1=auto;2=manual`, as the sensoCOMFORT itself names its modes
(time-controlled, manual, off; observed on the reference installation), a
circuit's setback mode `Hc<n>AutoOffMode` reads `0=eco;1=normal` (Eco/Normal in
the VRC 720/3 manual 0020334315_04), and the zone registers a thermostat shows
are polled (`r1`: OpMode, DayTemp, ActualRoomTempDesired, RoomTemp, SFMode).
In `vr_71.json` the controller's relay command `SetActorState` is also a
passive message (`uw`), so the relay states the controller sends become values
(observed on the reference installation: R6 switches the diverter valve with
every hot water charge). Keep this when the file is generated again.

What is a fact vs. an expression:

- Facts (free to use): a register's id, byte layout, datatype, divisor, unit and
  enumerated values — these describe how the device encodes reality.
- The protected element of `ebusd-configuration` is the *compilation* — the
  selection and arrangement of the whole database. We do not reproduce it; each
  device's def is the subset observed on our own bus, chosen by our own capture.

`vwzio.json` (0x76) holds only the current error: the device is recognised and
named, but its upstream CSV defines only service-test registers, and its
operational data travels over an undocumented low-level memory protocol that
neither ebusd nor this library decodes into named values.

## Registers taken from arotherm-genia-ebusd

The ids and meanings of these registers come from
[R91g/arotherm-genia-ebusd](https://github.com/R91g/arotherm-genia-ebusd), MIT
License, Copyright (c) 2026 R91g (full notice below). Each was checked against
the telegram log of the reference bus before it was added (values, and heat
output against flow x temperature difference x 1.163), and is kept as facts only:

- `hmu.json`: `RunDataHeatOutput`, `RunDataCondenserOutTemp`, `RunStatsDeiceHours`,
  `RunStatsDeiceStarts`; the `RunStats*` counters as four bytes
- `hmu.json`, `ctlv3.json`, `vwzio.json`: `Currenterror` (the first error)
- `ctlv3.json`: `SystemOperatingState`, `GlobalSystemOff`, `BivalencePointHc`,
  `BivalencePointHwc`, `AlternativePoint`, `BufferChargeOffset`
- `index.json`: the idents `HMUX0`, `BASV3` and `BASS3` (same registers per that
  project; not seen on the reference bus)

Not taken, as the reference bus contradicts it: `StateCompressor.modulation` as a
percentage (it reaches 200 at 110 rps) and `RunStatsImmersionHeaterHours` (13891 h
on a heat pump whose electric heater hardly ever ran).

```text
MIT License

Copyright (c) 2026 R91g

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
