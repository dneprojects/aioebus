# Changelog

## 0.2.1

- Fix a poll-scheduler starvation: a poll the device never answered kept its timer at zero, so it stayed the most-overdue item and the engine re-issued the same dead read forever — stalling every other register and the passive decode in between. A failed poll now records the attempt and waits its interval.

## 0.2.0

- Poll scheduler can poll plain `r` reads too (`default_priority`), not just `r1`..`r9`, so a consumer can refresh every readable register; `PollScheduler.for_registry` builds the set from a registry
- Registers are keyed per device by `(zz, name)`, so the same name on two devices schedules and suppresses independently
- Re-engineered own Vaillant device definitions (ctlv3/pmw/vr_71/hmu) plus a broadcast circuit, from an observed bus capture (see `configs/vaillant/PROVENANCE.md`); `tools/reengineer.py`

## 0.1.0 — first release

- eBUS datatype codecs (ints, fixed-point, floats, BCD/PIN, hex/string, bit fields, date/time), validated against real captures
- Message model + ebusd-CSV ingest + native JSON format with a converter
- Full datatype coverage and condition parsing for the stock Vaillant configs
- eBUS frame layer: escaping, CRC-8, telegram parsing incl. NAK retransmission (100% against 285k captured telegrams)
- Passive decode: raw wire bytes → named values
- Enhanced-adapter transport (host↔adapter protocol)
- Active reads and writes over the adapter (arbitration, echo/collision check, slave CRC), validated live
- Device scan (0704) and condition evaluation against scan data
- Poll scheduler (priority-weighted, passive-first) and a unified engine interleaving passive decode with active transactions
