# Provenance of the Vaillant device definitions

All definitions in `custom_components/ebus/configs/vaillant/` are our own Apache-2.0 work. None is a copy of the
GPL-3.0 `ebusd-configuration` database.

Two kinds live here:

- **Authored defs** — `38.v32` (eloBlock) and `f6.netx3`. Hand-written from
  observation of the reference installation, kept as CSV under `configs/vaillant/src/`
  and converted to JSON with aioebus `convert.csv_to_json` (`38.v32` became `eloblock.json`).

- **Re-engineered device types** — `ctlv3`, `pmw`, `vr_71`, `hmu` and
  `broadcast`. Produced by aioebus `tools/reengineer.py` from a captured telegram log of
  the reference bus. The tool uses an `ebusd-configuration` device CSV purely as
  a *decoding key* and keeps only the registers that actually carried a value on
  this installation — our own selection of observed facts, not upstream's full
  curated compilation. Retained from the CSV are facts only: message ids,
  datatypes, divisors, units, value tables, and the short functional field
  names. The source CSVs are **not** redistributed here.

What is a fact vs. an expression:

- Facts (free to use): a register's id, byte layout, datatype, divisor, unit and
  enumerated values — these describe how the device encodes reality.
- The protected element of `ebusd-configuration` is the *compilation* — the
  selection and arrangement of the whole database. We do not reproduce it; each
  device's def is the subset observed on our own bus, chosen by our own capture.

`vwzio` (0x76) yields no file: its upstream CSV defines only service-test
registers, and its operational data travels over an undocumented low-level
memory protocol that neither ebusd nor this library decodes into named values.
