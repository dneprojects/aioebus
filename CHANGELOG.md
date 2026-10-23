# Changelog

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
