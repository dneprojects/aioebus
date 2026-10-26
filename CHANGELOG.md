# Changelog

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
