"""Device catalog: which message definitions a discovered device has.

For each device the catalog takes, in this order:

1. our own bundled definition (``defs/vaillant``, Apache-2.0), chosen by scan
   ident and HW variant;
2. a user definition from a directory (native JSON or an ebusd CSV), chosen by
   scan ident;
3. the matching ebusd-configuration CSV from the public ebus.github.io CDN,
   chosen the way ebusd does (``ZZ.ident[.circuit][.SWnnnn][.HWnnnn].csv``:
   bus address, ident, SW/HW variant). It is downloaded on the user's own
   machine and kept in a cache directory (see :mod:`aioebus.cdn`); the library
   ships none of it.

Disk access runs in a thread. After :meth:`Catalog.load` everything is answered
from memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any, Final

from .cdn import CdnFiles, Fetch
from .conditions import ConditionContext, context_from_scan, evaluate_all
from .convert import from_dicts
from .frame import BROADCAST
from .messages import MessageDef, parse_csv
from .scan import Identification, ScanResult, config_folder
from .translations import attach_translations

_LOGGER = logging.getLogger(__name__)

BUNDLED_DIR: Final = Path(__file__).parent / "defs" / "vaillant"
# the broadcast circuit: telegrams to all (0xfe), resolved like a device
BROADCAST_SCAN: Final = ScanResult(0, "", "Broadcast", "", "", None, None)

_CDN_NAME = re.compile(r"^(?P<zz>[0-9a-f]{2})\.(?P<ident>[\w-]+)(?P<rest>(?:\.[\w-]+)*)\.csv$")
# a file name from the CDN (listing, include): no path, so the cache stays its own
_SAFE_NAME = re.compile(r"^[\w-]+(?:\.[\w-]+)*$")
_MAX_INCLUDE_DEPTH: Final = 3
# a load instruction with optional conditions in front: [cond][cond]!load,<file>
# (!load: the first one that applies wins; !include: every one that applies)
_LOAD = re.compile(r"^((?:\[[^\]\n]*\])*)!(load|include),([^,\s]+)", re.MULTILINE)


@dataclass(frozen=True)
class Definition:
    """Where a device's definition came from. ``file`` is the bundled file name,
    the user file name or the CDN path; ``profile`` is ``IDENT/HW`` when a
    bundled per-HW profile matched; ``text_paths`` are the CDN paths (without
    ``/<lang>/``) of the files that carry its display text."""
    source: str  # "bundled", "user" or "cdn"
    file: str
    profile: str | None = None
    model: str | None = None
    text_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Texts:
    """Messages with display texts: ``complete`` is False when some text file
    could not be reached (try again later); ``languages`` had any text."""
    messages: list[MessageDef]
    complete: bool
    languages: frozenset[str]


@dataclass
class ClimateZone:
    """A heating zone: the addressed messages for its target setpoint
    (read/write), room temperature and operating mode (read/write), its
    user-given short name, and the setpoint range. Missing roles are None.
    ``modes`` names the mode values for the roles "off", "auto" and "heat"."""
    zz: str
    n: int
    target_read: MessageDef | None
    target_write: MessageDef | None
    current: MessageDef | None
    mode_read: MessageDef | None
    mode_write: MessageDef | None
    shortname: MessageDef | None
    target_bounds: tuple[float, float, float] | None = None
    modes: dict[str, str] = field(default_factory=dict)


@cache
def bundled_index() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((BUNDLED_DIR / "index.json").read_text(encoding="utf-8"))
    return data


@cache
def bundled_file(name: str) -> tuple[MessageDef, ...]:
    """The messages of one bundled definition file (not addressed)."""
    return tuple(from_dicts(json.loads((BUNDLED_DIR / name).read_text(encoding="utf-8"))))


def bundled_files() -> list[str]:
    return sorted(p.name for p in BUNDLED_DIR.glob("*.json") if p.name != "index.json")


def bundled_file_of(key: str) -> str | None:
    """The bundled file a definition key stands for: a file name itself, or the
    file of an ``IDENT/HW`` profile (:attr:`Definition.profile`); None if the
    catalog has no such file or profile."""
    if "/" not in key:
        return key if key in bundled_files() else None
    ident, hw = key.split("/", 1)
    found = _profile(ident, hw)
    return found[0] if found is not None and found[1] == key else None


def _profile(ident: str, hw: str) -> tuple[str, str | None, dict[str, Any]] | None:
    """(file, profile key, profile) of a bundled device by ident and HW variant."""
    entry = bundled_index()["devices"].get(ident)
    key = None
    if isinstance(entry, dict) and "file" not in entry:
        by_hw = entry.get("hw", {})
        if hw in by_hw:
            entry, key = by_hw[hw], f"{ident}/{hw}"
        else:
            entry = entry.get("default")
    if isinstance(entry, str):
        return entry, key, {}
    if isinstance(entry, dict) and isinstance(entry.get("file"), str):
        return entry["file"], key, entry
    return None


def resolve_bundled(scan: ScanResult) -> Definition | None:
    found = _profile(scan.ident, scan.hw)
    if found is None:
        return None
    file, key, prof = found
    index = bundled_index()
    return Definition("bundled", file, key, prof.get("model") or index["models"].get(file),
                      tuple(f"{p}.csv" for p in index["cdn"].get(file, ())))


def _device_ident(scan: ScanResult) -> str:
    """The scan ident as ebusd compares it: letters, digits and '_', lower case."""
    return "".join(c for c in (scan.ident or "").lower() if c.isalnum() or c == "_")


def _ident_match(file_ident: str, ident: str) -> int:
    """How well a file's ident fits a device's (0: not at all), as ebusd judges
    it: equal to the device ident, or to it with trailing digits removed one at
    a time; the longer the matched part, the better."""
    file_ident = file_ident.lower()
    remain = ident
    while remain and len(remain) >= len(file_ident):
        if remain == file_ident:
            return len(remain) + 1
        if not remain[-1].isdigit():
            break
        remain = remain[:-1]
    return 0


def _variant_fits(tokens: Iterable[str], scan: ScanResult) -> bool:
    """Whether the variant tokens of a file name fit a device: a ``SWnnnn``/
    ``HWnnnn`` token must be its version; other tokens name a circuit."""
    for token in tokens:
        token = token.lower()
        if re.fullmatch(r"(sw|hw)\d{4}", token):
            want = scan.sw if token.startswith("sw") else scan.hw
            if token[2:] != (want or ""):
                return False
    return True


def _best_file(candidates: Iterable[tuple[str, str, list[str]]], scan: ScanResult) -> str | None:
    """Of (name, ident, variant tokens) candidates, the file ebusd would pick:
    the best ident match whose variant fits, then the longer (more specific)
    name."""
    ident = _device_ident(scan)
    if not ident:
        return None
    best: tuple[int, int, str] | None = None
    for name, file_ident, tokens in candidates:
        score = _ident_match(file_ident, ident)
        if score and _variant_fits(tokens, scan) and (
                best is None or (score, len(name)) > best[:2]):
            best = (score, len(name), name)
    return best[2] if best else None


def resolve_user_file(scan: ScanResult, user_dir: Path) -> Path | None:
    """A user definition for a device: ``ZZ.ident[.circuit][.SWnnnn][.HWnnnn]``
    (``.json`` or ``.csv``) chosen as ebusd chooses its files. The address part
    is not compared: identical devices behind different couplers share one file."""
    files = {p.name: p for p in sorted([*user_dir.glob("*.json"), *user_dir.glob("*.csv")])}
    candidates = []
    for name, p in files.items():
        parts = p.stem.split(".")  # ZZ . ident [. tokens]
        if len(parts) >= 2 and parts[1]:
            candidates.append((name, parts[1], parts[2:]))
    best = _best_file(candidates, scan)
    return files[best] if best else None


def match_cdn_file(names: Iterable[str], address: int, scan: ScanResult) -> str | None:
    """The CDN file ebusd would load for a device at an address (see
    :func:`resolve_user_file`), among the names of a CDN folder."""
    candidates = []
    for name in names:
        m = _CDN_NAME.match(name.lower())
        if m is not None and int(m["zz"], 16) == address:
            candidates.append((name, m["ident"], m["rest"].split(".")[1:]))
    return _best_file(candidates, scan)


def context_for(scan: ScanResult, ident: Identification | None = None) -> ConditionContext:
    """What conditions are evaluated against: the scan (id_sw, id_hw) and, when
    known, the product (article) number of the identification."""
    ctx = context_from_scan(scan)
    if ident is not None and ident.article:
        ctx.values["Scan_id_product"] = ident.article
    return ctx


def includes(text: str, ctx: ConditionContext) -> list[str]:
    """The files a CSV's instructions load for a device, as ebusd executes them
    in file order: of the ``!load`` lines the first one that applies (no
    condition, or all its conditions hold), and every ``!include`` that applies."""
    out: list[str] = []
    loaded = False
    for prefix, kind, name in _LOAD.findall(text):
        conditions = re.findall(r"\[([^\]]*)\]", prefix)
        if conditions and evaluate_all(conditions, ctx) is not True:
            continue
        if kind == "load":
            if loaded:
                continue
            loaded = True
        out.append(name)
    return list(dict.fromkeys(out))


async def _with_includes(name: str, text: str, ctx: ConditionContext,
                         read: Callable[[str], Awaitable[str | None]]
                         ) -> tuple[list[MessageDef], list[str]]:
    """A CSV's messages and those of the files it loads (and theirs, a few levels
    at most), read through ``read``; an include that cannot be read is skipped.
    Returns the messages and the names of the included files read."""
    messages = await asyncio.to_thread(parse_csv, text)
    seen, used = {name}, []
    pending = [(text, 0)]
    while pending:
        source, depth = pending.pop()
        for include in includes(source, ctx):
            if include in seen or depth >= _MAX_INCLUDE_DEPTH:
                continue
            seen.add(include)
            if not _SAFE_NAME.match(include):
                _LOGGER.warning("%s: include %r ignored", name, include)
                continue
            extra = await read(include)
            if extra:
                messages += await asyncio.to_thread(parse_csv, extra)
                used.append(include)
                pending.append((extra, depth + 1))
    return messages, used


def _command(m: MessageDef) -> tuple[str, str, bool, bool]:
    """What identifies a message on a device, as in ebusd: its command (PBSB +
    id) and its kind -- a read and a write of one register are two messages, and
    so are an active and a passive one."""
    return (m.pbsb.lower(), m.msgid.lower(), m.is_write, m.is_passive)


def addressed(address: int, scan: ScanResult, messages: Iterable[MessageDef],
              ctx: ConditionContext | None = None, *,
              keep_row_address: bool = False) -> list[MessageDef]:
    """A device's messages with its address stamped on. Conditions are evaluated
    against this device (``ctx``, by default its scan): a message whose condition
    is false is dropped, and where several define one command the best fit wins
    (a matched condition over an unconditional base over an undecided one).
    With ``keep_row_address`` a message that names its own destination keeps it
    (an ebusd CSV row with a ZZ column, e.g. a register of a module at another
    address); otherwise the device address replaces it."""
    zz = f"{address:02x}"
    ctx = ctx if ctx is not None else context_from_scan(scan)
    chosen: dict[tuple[str, str, bool, bool], tuple[int, MessageDef]] = {}
    for m in messages:
        result = evaluate_all(m.conditions, ctx)
        if result is False:
            continue
        rank = (3 if result is True else 1) if m.conditions else 2
        key = _command(m)
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, m)
    return [m if keep_row_address and m.zz else replace(m, zz=zz)
            for _, m in chosen.values()]


class Catalog:
    """The definitions of the devices on one bus (see the module docstring)."""

    def __init__(self, *, user_dir: Path | None = None, cache_dir: Path | None = None,
                 fetch: Fetch | None = None, lang: str = "en") -> None:
        self.user_dir = user_dir
        self._cdn = CdnFiles(cache_dir=cache_dir, fetch=fetch, lang=lang)
        self._scans: dict[int, ScanResult] = {}
        self._defs: dict[int, Definition] = {}
        self._messages: dict[int, list[MessageDef]] = {}
        self._owner: dict[int, int] = {}  # id(message) -> device address

    @property
    def cache_dir(self) -> Path | None:
        """Where downloaded definitions are kept (None: not kept)."""
        return self._cdn.cache_dir

    @property
    def lang(self) -> str:
        """The language of the definition files read from the CDN."""
        return self._cdn.lang

    async def load(self, devices: Mapping[int, ScanResult],
                   idents: Mapping[int, Identification] | None = None) -> None:
        """Resolve every device's definition (bundled, user, CDN); devices without
        any keep no messages. The broadcast circuit is always included. ``idents``
        (the Vaillant identifications) let product-specific parts be chosen."""
        self._scans = {**devices, BROADCAST: BROADCAST_SCAN}
        self._defs, self._messages, self._owner = {}, {}, {}
        self._cdn.forget_listings()
        await asyncio.to_thread(bundled_index)
        for address, scan in self._scans.items():
            ctx = context_for(scan, (idents or {}).get(address))
            try:
                found = await self._resolve(address, scan, ctx)
            except Exception:  # one broken definition costs its device, never the bus
                _LOGGER.exception("definition for %s at %#04x failed", scan.ident, address)
                found = None
            if found is None:
                _LOGGER.info("no definition for %s at %#04x", scan.ident, address)
                continue
            definition, messages = found
            self._defs[address] = definition
            # a CSV row may name its own destination; our JSON files carry the
            # address they were made from, which the device's replaces
            msgs = addressed(address, scan, messages, ctx,
                             keep_row_address=definition.file.lower().endswith(".csv"))
            self._messages[address] = msgs
            self._owner.update((id(m), address) for m in msgs)

    async def _resolve(self, address: int, scan: ScanResult, ctx: ConditionContext
                       ) -> tuple[Definition, list[MessageDef]] | None:
        if (bundled := resolve_bundled(scan)) is not None:
            return bundled, list(await asyncio.to_thread(bundled_file, bundled.file))
        if self.user_dir is not None:
            path = await asyncio.to_thread(resolve_user_file, scan, self.user_dir)
            if path is not None:
                try:
                    messages = await _read_user(path, ctx)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
                    # one broken file costs its device, never the whole bus
                    _LOGGER.warning("definition file %s skipped: %s", path.name, err)
                else:
                    return Definition("user", path.name), messages
        folder = config_folder(scan.mf)
        if folder is None or not self._cdn.usable:
            return None
        names = await self._cdn.listing(folder)
        name = match_cdn_file(names or (), address, scan)
        if name is None:
            return None
        text = await self._cdn.text(f"{folder}/{name}")
        if not text:
            return None

        async def read(include: str) -> str | None:  # best effort: a missing one is skipped
            return await self._cdn.text(f"{folder}/{include}")

        messages, used = await _with_includes(name, text, ctx, read)
        paths = tuple(f"{folder}/{n}" for n in (name, *used))
        return Definition("cdn", f"{folder}/{name}", text_paths=paths), messages

    @property
    def text_paths(self) -> list[str]:
        """The CDN paths (without ``/<lang>/``) of the files with the display
        text of the loaded devices."""
        return sorted({p for d in self._defs.values() for p in d.text_paths})

    async def load_texts(self, langs: Iterable[str]) -> Texts:
        """The loaded messages, as copies carrying their display texts (``tr``)
        in the given languages, from the CDN (cached like the definitions)."""
        by_lang: dict[str, list[MessageDef]] = {}
        complete = True
        paths = self.text_paths
        for lang in dict.fromkeys(langs):
            # one language's files at once: a slow one does not hold up the rest
            states = await asyncio.gather(*(self._cdn.text_state(p, lang) for p in paths))
            for text, current in states:
                # not reachable (even if a stale copy stood in): try again later;
                # "" is final, e.g. a language upstream does not translate
                complete = complete and current
                if text:
                    by_lang.setdefault(lang, []).extend(await asyncio.to_thread(parse_csv, text))
        msgs = [replace(m, fields=[replace(f) for f in m.fields]) for m in self.all_messages()]
        if by_lang:
            attach_translations(msgs, by_lang)
        return Texts(msgs, complete, frozenset(by_lang))

    # -- answers from memory ------------------------------------------------

    def definition(self, address: int) -> Definition | None:
        return self._defs.get(address)

    def scan(self, address: int) -> ScanResult | None:
        return self._scans.get(address)

    @property
    def addresses(self) -> list[int]:
        return list(self._scans)

    def messages(self, address: int) -> list[MessageDef]:
        """The addressed messages of one device."""
        return list(self._messages.get(address, ()))

    def all_messages(self) -> list[MessageDef]:
        return [m for msgs in self._messages.values() for m in msgs]

    def owner(self, msg: MessageDef) -> int:
        """The address of the device whose definition holds the message (it may
        be sent to another address, see :func:`addressed`)."""
        return self._owner.get(id(msg), int(msg.zz, 16) if msg.zz else BROADCAST)

    def device_messages(self, direction: str) -> list[MessageDef]:
        """Messages of a direction ('r' read incl. passive updates, 'w' active
        write) across all devices, one per (address, name) -- the first
        definition wins."""
        want_read = direction == "r"
        out: list[MessageDef] = []
        seen: set[tuple[str, str]] = set()
        for m in self.all_messages():
            if want_read != m.is_read or (not want_read and m.direction != "w"):
                continue
            if (m.zz, m.name) not in seen:
                seen.add((m.zz, m.name))
                out.append(m)
        return out

    def read_twin(self, msg: MessageDef) -> MessageDef | None:
        """The active read of the register a write message sets (to read a
        written value back), if the device has one."""
        return next((m for m in self._messages.get(self.owner(msg), ())
                     if m.name == msg.name and m.zz == msg.zz and m.is_polled_read), None)

    def bounds(self, address: int, message: str) -> tuple[float, float, float] | None:
        """(min, max, step) of a writable register as the bundled catalog lists it."""
        definition = self._defs.get(address)
        if definition is None or definition.source != "bundled":
            return None
        listed = bundled_index()["number_bounds"].get(definition.file, {}).get(message)
        if isinstance(listed, list) and len(listed) == 3:
            return (float(listed[0]), float(listed[1]), float(listed[2]))
        return None

    def zones(self) -> list[ClimateZone]:
        """Heating zones: '<prefix>N<target/current/mode>' registers bundled per
        zone N, as the catalog's 'climate' section declares for a device's file."""
        out: list[ClimateZone] = []
        for address, definition in self._defs.items():
            decl = (bundled_index()["climate"].get(definition.file)
                    if definition.source == "bundled" else None)
            if not isinstance(decl, dict):
                continue
            prefix = str(decl.get("zone_prefix", ""))
            target, current, mode = decl["target"], decl["current"], decl["mode"]
            roles = decl.get("modes", {})
            msgs = self._messages.get(address, [])
            rx = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(target)}$")
            for n in sorted({int(mt.group(1)) for m in msgs
                             if (mt := rx.match(m.name)) and m.is_write}):
                tw = _find(msgs, f"{prefix}{n}{target}", write=True)
                mode_read = _find(msgs, f"{prefix}{n}{mode}", write=False)
                mode_write = _find(msgs, f"{prefix}{n}{mode}", write=True)
                out.append(ClimateZone(
                    zz=f"{address:02x}", n=n,
                    target_read=_find(msgs, f"{prefix}{n}{target}", write=False),
                    target_write=tw,
                    current=_find(msgs, f"{prefix}{n}{current}", write=False),
                    mode_read=mode_read,
                    mode_write=mode_write,
                    shortname=_find(msgs, f"{prefix}{n}Shortname", write=False),
                    target_bounds=self.bounds(address, tw.name) if tw else None,
                    modes=_mode_roles(roles, mode_read or mode_write),
                ))
        return out


def _find(msgs: list[MessageDef], name: str, *, write: bool) -> MessageDef | None:
    """The message of that name and direction, an active one before a passive
    one (it can be sent)."""
    hits = [m for m in msgs if m.name == name and (m.is_write if write else m.is_read)]
    return next((m for m in hits if not m.is_passive), hits[0] if hits else None)


def _mode_roles(roles: object, msg: MessageDef | None) -> dict[str, str]:
    """The declared mode roles whose value the register actually has."""
    labels = {v for f in (msg.fields if msg else ()) if f.values for v in f.values.values()}
    if not isinstance(roles, dict):
        return {}
    return {role: label for role, label in roles.items()
            if isinstance(label, str) and label in labels}


async def _read_user(path: Path, ctx: ConditionContext) -> list[MessageDef]:
    """A user definition; an ebusd CSV with its includes (same folder)."""
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    if path.suffix.lower() != ".csv":
        return from_dicts(json.loads(text))

    async def read(include: str) -> str | None:  # one the folder lacks is skipped
        try:
            return await asyncio.to_thread((path.parent / include).read_text, encoding="utf-8")
        except OSError:
            return None

    messages, _used = await _with_includes(path.name, text, ctx, read)
    return messages
