# Changelog

## 0.6.0

- Writes follow the eBUS telegram shape of their destination: a slave's answer is received and acknowledged before the bus is released (a SYN went out right after the ACK); master/master ends after the ACK, a broadcast expects none
- A NAKed command is repeated once from QQ, a slave answer failing its CRC is NAKed once so it is repeated (as the spec and ebusd do)
- A write is never sent twice once it may have landed: an ACKed write counts as applied even if its answer fails (also when the ACK arrives late), and one whose whole command went out unconfirmed raises the new `WriteUncertain` (a `ReadError`) instead of being retried
- `EXP`/`EXR` values can be written; out-of-range numbers, a type's replacement value, `None` and non-finite values are rejected instead of wrapping or sending "no value"; rounding is half away from zero like ebusd
- `MessageDef.encode` takes the message name for its single value field whatever that field is called
- Engine: explicit requests start only with time to finish (`START_MARGIN`); a reconnect fails an in-flight request at once; `read()` refuses non-read messages; at most 16 master data bytes

## 0.5.3

- Fix: a read the slave ACKs but leaves unanswered is retried (up to 3 attempts) again; 0.5.2 failed it at once, so e.g. a device's identification could go missing

## 0.5.2

- Fix: an active transaction now has a deadline (`EbusEngine.ACTIVE_DEADLINE`, 3 s) — one the bus never completed held the engine in the active state forever, stopping passive decoding and all polls
- Fix: a read the slave ACKs but never answers fails at the next SYN instead of waiting for a phantom 0xAA-length answer
- Fix: a slave answer whose last byte is escaped (e.g. CRC 0xAA sent as `A9 01`) was judged complete on the escape prefix and failed the CRC
- Fix: another master's unanswered read no longer decodes to `None` values that overwrite the cache and suppress the poll
- Fix: an explicit read/write whose caller timed out is never sent later — withdrawn from the queue, not retried after a lost arbitration, and not started without time left to finish (`read`/`write` default timeout now 6 s)
- Fix: an empty answer (SN=0) to one of our own reads counts as unanswered instead of caching `None`
- `ActiveReader.read_identification` re-raises a lost link (`ConnectionError`) instead of reporting "no identification"
- `PollScheduler.carry_from(old)` keeps register timing across a poll-set rebuild, so re-scoping polling causes no burst

## 0.5.1

- Fix: `EXP`/`EXR` (IEEE binary32) float fields now honor the CSV divisor column, like the integer types already did — it was silently ignored, so a value such as `b516`/`10` lifetime energy decoded 1000x too large (raw instead of kWh)

## 0.5.0

- Read the Vaillant extended identification: `ActiveReader.read_identification` (from `b509` 24/25/26/27) and `scan.decode_identification` yield the 10-digit article number (Sachnummer, which maps to the actual model, unlike the shared control-board id), the full 28-character serial (character-for-character as the Vaillant app shows it) and the manufacture year/week. Validated against a sensoCOMFORT VRC 720 (article 0010045478) and four real app serials

## 0.4.0

- Definitions can carry embedded display-text translations: `MessageDef.tr` / `FieldDef.tr` (`{lang: text}`), round-tripped through the JSON format; `translations.localized(tr, lang, fallback)` resolves a language with English-then-technical fallback. Lets one definition serve bundled and imported devices in any installed language, with the technical `name` staying the stable key
- Merge a device's per-language CSVs into one definition carrying `tr`: `convert.merge_languages({lang: csv_text})` / `merge_languages_to_json` / `convert_langs_dir` (ebus.github.io `/en/`, `/de/` layout), `convert.attach_translations`. `tools/reengineer.py` gains `--tr LANG CSV...` to attach translations to re-engineered definitions

## 0.3.0

- Engine exposes an `EngineStats` surface (`engine.stats`): link `connected` flag, count of telegrams decoded off the bus, answered/unanswered scheduled polls, active writes, and the time of the last bus telegram — so a consumer can present the adapter's own connection and activity as entities

## 0.2.2

- Capture the telegram the engine loses arbitration to, instead of discarding it: the winner's address is known from the failed arbitration, so the engine seeds the passive buffer with it and decodes the whole telegram at the next SYN. Broadcasts (e.g. outside temperature) and other masters' reads are now decoded passively even while we are trying to poll
- Back off a persistently unanswered poll: each consecutive no-answer doubles its interval (up to 32x), so a dead register fades out of the rotation instead of being retried every cycle

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
