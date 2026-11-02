# Changelog

## 0.7.3

- Catalog: a read and a write (or an active and a passive message) sharing one command are both kept, as in ebusd; before, one was dropped (e.g. the bus date/time write, the heat pump's SetMode write, timer writes)
- Catalog: an ebusd CSV row that names its own destination keeps it instead of being sent to the device's address
- Catalog: `!load` follows ebusd (the first that applies wins, `!include` supported); device idents match as in ebusd (only trailing digits dropped); display texts include the included files; a raising `fetch` counts as unreachable
- Catalog: `read_twin()`, `owner()`; heating zones name their mode values per role (`ClimateZone.modes`), the display name is the application's
- Passive decoding prefers the definition whose shape fits the telegram (read, write, passive); variant choice by conditions is the catalog's alone (`MessageRegistry` takes no context)
- ebusd CSV default rows as in ebusd: per message type, PBSB with ID prefix, default fields first, `*` in a default
- Deep scan probes every slave address (228), e.g. VR 70 at 0x52
- Reads that need request values (`MessageDef.needs_args`) are not polled; `EbusEngine.read(args=...)` sends them
- Engine: queued requests fail at once when the link ends, and new ones while it is down
- Poll: a register never polled is due at once, also right after boot
- A definition with a chained id (unsupported) is skipped instead of failing every telegram of its command
- `STR` ends at the first NUL as in ebusd; `HEX:*`/`IGN:*` take the rest
- Manufacturer names and config folders from one table (ebusd's)
- New `link` module: `connect()` and `probe()` for the adapter connection
- `values`: `value_key()` and `Value.cache_key`, `closes_period()`; `messages.label_of()` is the one label rule; `control_of()` only for active writes
- Removed the second transaction path (`ActiveReader`, `ActiveWriter`, `build_read`, `PassiveListener`, `listen`) and unused helpers (`translations.localized`, `_slug`, `PollScheduler.for_registry`, `convert.merge_languages*`, `convert_langs_dir`, `reengineer.py --tr`)
- `__all__` lists the public API; mypy runs in CI; README, provenance and docstrings brought up to date

## 0.7.2

- New `values` module: `values_of()` (each named field with its label and stable `key`), `control_of()` (the number/select a write allows), `is_number_type()`, `has_numeric_value()`
- `Catalog.load_texts()` gives copies of the loaded messages with their display texts per language from the CDN, cached like the definitions; `Catalog.text_paths`; the caller's `fetch` returns `""` for a file the CDN does not have (final) and None when unreachable

## 0.7.1

- New `catalog` module: `Catalog.load()` gives each discovered device its definitions -- our bundled ones (now package data, with the device index, models, heating-zone roles and register ranges), else a user file (JSON or ebusd CSV), else the matching ebusd-configuration CSV downloaded from ebus.github.io (ebusd's address/ident/variant rule, `!load` includes) and cached; network through a caller-given `fetch`, disk in a thread
- CDN details: conditional `!load` includes chosen by HW version or product number (also in user CSVs and nested), one listing per folder and load, non-CSV answers never cached, a broken definition costs only its device
- `Catalog` answers from memory: messages per device with conditions resolved, read/write lists, `zones()`, `bounds()`, each device's `Definition` (source, file, profile, model, text paths)

## 0.7.0

- New `discovery` module: `discover()` scans the bus through the running engine (waits for traffic, base or deep address space, Vaillant identification, remembered identifications of the same unit, phantom addresses dropped by serial) and `identify_later()` reads missed identifications at runtime; `BusSilent` when the bus stays quiet
- Engine: `scan()`, `wait_for_traffic()` (ends early when the link drops), `running`; explicit reads are tried again when the slave skips its answer (as `ActiveReader` did)

## 0.6.3

- `EbusEngine.read_identification()` reads the Vaillant identification through the running engine; `scan.VAILLANT`, `IDENT_PBSB`, `IDENT_MSGIDS` are its single home
- `MessageDef.value_fields` and `MessageDef.is_polled_read`; `translations.slug()` is public (`_slug` kept as alias)
- Removed `translations.ha_strings()` (superseded by the integration's generator) and the stale `configs/` copy with the tools bound to it (`active_read`, `active_write`, `live_listen`, `convert_configs`); the definitions live in the ebus integration
- Version single-sourced from `aioebus.__version__`; stale docstrings corrected

## 0.6.2

- Engine: a failure while handling one telegram, answer or callback fails only the transaction in flight, never the bus loop; a send error ends the loop like a receive error
- Engine: when the link ends, the request in flight fails at once (`WriteUncertain` for a write that may have landed, success for one already ACKed) instead of at its timeout; a poll that fails internally is backed off
- Engine: a STARTED nobody waits for (a cancel that came too late) releases the bus with a SYN
- Engine: `read()`/`write()` reject a malformed request (PBSB, id, more than 16 data bytes) and a timeout shorter than one transaction in the call
- An arbitration given up at its deadline is withdrawn at the adapter (START with SYN); telegrams passing while arbitrating are decoded
- `ActiveReader`/`ActiveWriter` raise at EOF instead of spinning the event loop
- Definitions with a type this library cannot decode are skipped (logged); repeated field names get `_1`, `_2` suffixes; reads without a device address are not polled
- Codecs: 24:00 is a valid time, D1C ends at 100.0, TTH/TTQ use their low 6/7 bits, DTM out of range gives no value and prints `dd.mm.yyyy hh:mm` like ebusd
- New `codecs.is_numeric()` and `codecs.value_range()` (min, max, step of an integer type)
- Enh decoder keeps short-form bus bytes after a desync; conditions compare numbers with a divisor and leave a label against a number undecided
- CI also tests Python 3.14

## 0.6.1

- `EbusEngine.read_raw()` reads a register without a definition and returns the slave's raw answer (e.g. the identification at runtime)

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
