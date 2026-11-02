"""Device catalog: which message definitions a discovered device has.

For each device the catalog takes, in this order:

1. our own bundled definition (``defs/vaillant``, Apache-2.0), chosen by scan
   ident and HW variant;
2. a user definition from a directory (native JSON or an ebusd CSV), chosen by
   scan ident;
3. the matching ebusd-configuration CSV from the public ebus.github.io CDN,
   chosen the way ebusd does (``ZZ.ident[.circuit][.SWnnnn][.HWnnnn].csv``:
   bus address, ident prefix, SW/HW variant). It is downloaded on the user's own
   machine and kept in a cache directory; the library ships none of it.

Network access goes through a ``fetch`` callable the caller provides (so an
application can use its own HTTP session); disk access runs in a thread. After
:meth:`Catalog.load` everything is answered from memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any, Final

from .conditions import ConditionContext, context_from_scan, evaluate_all
from .convert import from_dicts
from .messages import MessageDef, parse_csv
from .scan import Identification, ScanResult

_LOGGER = logging.getLogger(__name__)

BUNDLED_DIR: Final = Path(__file__).parent / "defs" / "vaillant"
CDN: Final = "https://ebus.github.io"
# manufacturer byte -> the CDN folder with its devices
MANUFACTURER_FOLDERS: Final[dict[int, str]] = {
    0xB5: "vaillant", 0x19: "wolf", 0x50: "kromschroeder", 0x40: "encon", 0x10: "tem"}
CACHE_MAX_AGE: Final = 7 * 24 * 3600  # s; older cached files are fetched again
# the broadcast circuit: telegrams to all (0xfe), resolved like a device
BROADCAST_SCAN: Final = ScanResult(0, "", "Broadcast", "", "", None, None)

# returns the text at a URL, or None when it is not there / not reachable
Fetch = Callable[[str], Awaitable[str | None]]

_CDN_NAME = re.compile(r"^(?P<zz>[0-9a-f]{2})\.(?P<ident>[\w-]+)(?P<rest>(?:\.[\w-]+)*)\.csv$")
# a file name from the CDN (listing, include): no path, so the cache stays its own
_SAFE_NAME = re.compile(r"^[\w-]+(?:\.[\w-]+)*$")
_LISTING: Final = ".listing"  # cache name of a folder listing (no CDN name looks so)
_MAX_INCLUDE_DEPTH: Final = 3
# a load instruction with optional conditions in front: [cond][cond]!load,<file>
_LOAD = re.compile(r"^((?:\[[^\]\n]*\])*)!load,([^,\s]+)", re.MULTILINE)


@dataclass(frozen=True)
class Definition:
    """Where a device's definition came from. ``file`` is the bundled file name,
    the user file name or the CDN path; ``profile`` is ``IDENT/HW`` when a
    bundled per-HW profile matched; ``text_paths`` are the CDN paths (without
    ``/<lang>/`` and ``.csv``) that carry its display text."""
    source: str  # "bundled", "user" or "cdn"
    file: str
    profile: str | None = None
    model: str | None = None
    text_paths: tuple[str, ...] = ()


@dataclass
class ClimateZone:
    """A heating zone: the addressed messages for its target setpoint
    (read/write), room temperature and operating mode (read/write), its
    user-given short name, and the setpoint range. Missing roles are None."""
    zz: str
    n: int
    name: str  # fallback name; the zone's own short name wins where set
    target_read: MessageDef | None
    target_write: MessageDef | None
    current: MessageDef | None
    mode_read: MessageDef | None
    mode_write: MessageDef | None
    shortname: MessageDef | None
    target_bounds: tuple[float, float, float] | None = None


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


def _profile(scan: ScanResult) -> tuple[str, str | None, dict[str, Any]] | None:
    """(file, profile key, profile) of a bundled device by ident and HW variant."""
    entry = bundled_index()["devices"].get(scan.ident)
    key = None
    if isinstance(entry, dict) and "file" not in entry:
        by_hw = entry.get("hw", {})
        if scan.hw in by_hw:
            entry, key = by_hw[scan.hw], f"{scan.ident}/{scan.hw}"
        else:
            entry = entry.get("default")
    if isinstance(entry, str):
        return entry, key, {}
    if isinstance(entry, dict) and isinstance(entry.get("file"), str):
        return entry["file"], key, entry
    return None


def resolve_bundled(scan: ScanResult) -> Definition | None:
    found = _profile(scan)
    if found is None:
        return None
    file, key, prof = found
    index = bundled_index()
    return Definition("bundled", file, key, prof.get("model") or index["models"].get(file),
                      tuple(index["cdn"].get(file, ())))


def _tokens_fit(tokens: Iterable[str], scan: ScanResult) -> tuple[bool, int]:
    """Whether the variant tokens of a file name fit a device (a ``SWnnnn``/
    ``HWnnnn`` token must be its version; other tokens name a circuit), and how
    many such constraints the name has."""
    specific, ok = 0, True
    for token in tokens:
        token = token.lower()
        if re.fullmatch(r"(sw|hw)\d{4}", token):
            want = scan.sw if token.startswith("sw") else scan.hw
            ok = ok and token[2:] == (want or "")
            specific += 1
    return ok, specific


def resolve_user_file(scan: ScanResult, user_dir: Path) -> Path | None:
    """A user definition for a device: ``ZZ.ident[.circuit][.SWnnnn][.HWnnnn]``
    (``.json`` or ``.csv``) whose ident starts the scan ident and whose SW/HW
    variant (if any) is the device's; the longest ident and the more specific
    variant win. The address part is not compared: identical devices behind
    different couplers share one file."""
    ident = (scan.ident or "").lower()
    if not ident:
        return None
    best: tuple[int, int, Path] | None = None
    for p in sorted([*user_dir.glob("*.json"), *user_dir.glob("*.csv")]):
        parts = p.stem.split(".")  # ZZ . ident [. tokens]
        if len(parts) < 2 or not parts[1] or not ident.startswith(parts[1].lower()):
            continue
        ok, specific = _tokens_fit(parts[2:], scan)
        if ok and (best is None or (len(parts[1]), specific) > best[:2]):
            best = (len(parts[1]), specific, p)
    return best[2] if best else None


def match_cdn_file(names: Iterable[str], address: int, scan: ScanResult) -> str | None:
    """The CDN file ebusd would load for a device: ``ZZ.ident[.circuit][.SWnnnn]
    [.HWnnnn].csv`` at the device's address, whose ident starts the scan ident and
    whose SW/HW variant (if any) is the device's; the longest ident and the more
    specific variant win."""
    ident = (scan.ident or "").lower()
    best: tuple[int, int, str] | None = None
    for name in names:
        m = _CDN_NAME.match(name.lower())
        if m is None or int(m["zz"], 16) != address or not ident.startswith(m["ident"]):
            continue
        ok, specific = _tokens_fit(m["rest"].split(".")[1:], scan)
        if ok and (best is None or (len(m["ident"]), specific) > best[:2]):
            best = (len(m["ident"]), specific, name)
    return best[2] if best else None


def context_for(scan: ScanResult, ident: Identification | None = None) -> ConditionContext:
    """What conditions are evaluated against: the scan (id_sw, id_hw) and, when
    known, the product (article) number of the identification."""
    ctx = context_from_scan(scan)
    if ident is not None and ident.article:
        ctx.values["Scan_id_product"] = ident.article
    return ctx


def includes(text: str, ctx: ConditionContext) -> list[str]:
    """The files a CSV's ``!load`` lines ask for: those whose conditions hold for
    the device; when none of the conditional ones does, the unconditional ones
    (the file's default)."""
    chosen, default = [], []
    for prefix, name in _LOAD.findall(text):
        conditions = re.findall(r"\[([^\]]*)\]", prefix)
        if not conditions:
            default.append(name)
        elif evaluate_all(conditions, ctx) is True:
            chosen.append(name)
    return list(dict.fromkeys(chosen or default))


def addressed(address: int, scan: ScanResult, messages: Iterable[MessageDef],
              ctx: ConditionContext | None = None) -> list[MessageDef]:
    """A device's messages with its address stamped on. Conditions are evaluated
    against this device (``ctx``, by default its scan): a message whose condition
    is false is dropped, and where several share a command (pbsb+id) the best fit
    wins (a matched condition over an unconditional base over an undecided one)."""
    zz = f"{address:02x}"
    ctx = ctx if ctx is not None else context_from_scan(scan)
    chosen: dict[tuple[str, str], tuple[int, MessageDef]] = {}
    for m in messages:
        result = evaluate_all(m.conditions, ctx)
        if result is False:
            continue
        rank = (3 if result is True else 1) if m.conditions else 2
        key = (m.pbsb, m.msgid)
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, m)
    return [replace(m, zz=zz) for _, m in chosen.values()]


class Catalog:
    """The definitions of the devices on one bus (see the module docstring)."""

    def __init__(self, *, user_dir: Path | None = None, cache_dir: Path | None = None,
                 fetch: Fetch | None = None, lang: str = "en") -> None:
        self.user_dir = user_dir
        self.cache_dir = cache_dir
        self.lang = lang
        self._fetch = fetch
        self._scans: dict[int, ScanResult] = {}
        self._defs: dict[int, Definition] = {}
        self._messages: dict[int, list[MessageDef]] = {}
        self._listings: dict[str, list[str] | None] = {}  # per load: folder -> names

    async def load(self, devices: Mapping[int, ScanResult],
                   idents: Mapping[int, Identification] | None = None) -> None:
        """Resolve every device's definition (bundled, user, CDN); devices without
        any keep no messages. The broadcast circuit is always included. ``idents``
        (the Vaillant identifications) let product-specific parts be chosen."""
        self._scans = {**devices, 0xFE: BROADCAST_SCAN}
        self._defs, self._messages, self._listings = {}, {}, {}
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
            self._messages[address] = addressed(address, scan, messages, ctx)

    async def _resolve(self, address: int, scan: ScanResult, ctx: ConditionContext
                       ) -> tuple[Definition, list[MessageDef]] | None:
        if (bundled := resolve_bundled(scan)) is not None:
            return bundled, list(await asyncio.to_thread(bundled_file, bundled.file))
        if self.user_dir is not None:
            path = await asyncio.to_thread(resolve_user_file, scan, self.user_dir)
            if path is not None:
                try:
                    messages = await asyncio.to_thread(_read_user, path, ctx)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
                    # one broken file costs its device, never the whole bus
                    _LOGGER.warning("definition file %s skipped: %s", path.name, err)
                else:
                    return Definition("user", path.name), messages
        folder = MANUFACTURER_FOLDERS.get(scan.mf)
        if folder is None or (self._fetch is None and self.cache_dir is None):
            return None
        names = await self._listing(folder)
        name = match_cdn_file(names or (), address, scan)
        if name is None:
            return None
        text = await self._text(f"{folder}/{name}", name)
        if text is None:
            return None
        messages = await asyncio.to_thread(parse_csv, text)
        seen = {name}
        pending = [(text, 0)]
        while pending:  # includes, and theirs (a few levels at most)
            source, depth = pending.pop()
            for include in includes(source, ctx):
                if include in seen or depth >= _MAX_INCLUDE_DEPTH:
                    continue
                seen.add(include)
                if not _SAFE_NAME.match(include):
                    _LOGGER.warning("%s: include %r ignored", name, include)
                    continue
                extra = await self._text(f"{folder}/{include}", include)
                if extra:  # best effort: an include the CDN does not serve is skipped
                    messages += await asyncio.to_thread(parse_csv, extra)
                    pending.append((extra, depth + 1))
        path = f"{folder}/{name[:-len('.csv')]}"
        return Definition("cdn", f"{folder}/{name}", text_paths=(path,)), messages

    async def _listing(self, folder: str) -> list[str] | None:
        """A CDN folder's file names, fetched once per load (an unreachable CDN
        then costs one timeout, not one per device)."""
        if folder not in self._listings:
            text = await self._text(f"{folder}/", _LISTING)
            self._listings[folder] = text.split("\n") if text is not None else None
        return self._listings[folder]

    async def _text(self, rel: str, cache_name: str) -> str | None:
        """A CDN file (a folder listing for a path ending in '/'), from the cache
        while it is fresh, else fetched (and cached); a stale copy stands in when
        the CDN cannot be reached or answers with something else."""
        folder = rel.split("/", 1)[0]
        cached = (self.cache_dir / self.lang / folder / cache_name) if self.cache_dir else None
        stale: str | None = None
        if cached is not None:
            age_text = await asyncio.to_thread(_read_cached, cached)
            if age_text is not None:
                age, stale = age_text
                if age < CACHE_MAX_AGE:
                    return stale
        if self._fetch is None:
            return stale
        text = await self._fetch(f"{CDN}/{self.lang}/{rel}")
        if text is None:
            return stale
        if rel.endswith("/"):  # a folder listing is a JSON string of names
            try:
                listing = json.loads(text)
            except ValueError:
                listing = None
            if not isinstance(listing, str):
                return stale
            text = listing
        elif not text.lstrip().startswith("type,"):
            # not an ebusd CSV (e.g. a captive portal's page): never cache that
            _LOGGER.warning("%s: unexpected content, ignored", rel)
            return stale
        if cached is not None:
            await asyncio.to_thread(_write_cached, cached, text)
        return text

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

    def device_messages(self, direction: str) -> list[MessageDef]:
        """Messages of a direction ('r' read incl. passive updates, 'w' write)
        across all devices, one per (address, name) -- the first definition wins."""
        want_read = direction == "r"
        out: list[MessageDef] = []
        seen: set[tuple[str, str]] = set()
        for m in self.all_messages():
            if want_read != m.is_read or (not want_read and not m.is_write):
                continue
            if (m.zz, m.name) not in seen:
                seen.add((m.zz, m.name))
                out.append(m)
        return out

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
            msgs = self._messages.get(address, [])
            rx = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(target)}$")
            for n in sorted({int(mt.group(1)) for m in msgs
                             if (mt := rx.match(m.name)) and m.is_write}):
                tw = _find(msgs, f"{prefix}{n}{target}", write=True)
                out.append(ClimateZone(
                    zz=f"{address:02x}", n=n, name=str(decl.get("name", "Zone {n}")).format(n=n),
                    target_read=_find(msgs, f"{prefix}{n}{target}", write=False),
                    target_write=tw,
                    current=_find(msgs, f"{prefix}{n}{current}", write=False),
                    mode_read=_find(msgs, f"{prefix}{n}{mode}", write=False),
                    mode_write=_find(msgs, f"{prefix}{n}{mode}", write=True),
                    shortname=_find(msgs, f"{prefix}{n}Shortname", write=False),
                    target_bounds=self.bounds(address, tw.name) if tw else None,
                ))
        return out


def _find(msgs: list[MessageDef], name: str, *, write: bool) -> MessageDef | None:
    for m in msgs:
        if m.name == name and (m.is_write if write else m.is_read):
            return m
    return None


def _read_user(path: Path, ctx: ConditionContext | None = None) -> list[MessageDef]:
    """A user definition; an ebusd CSV with its ``!load`` includes (same folder)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".csv":
        return from_dicts(json.loads(text))
    messages = parse_csv(text)
    seen, pending = {path.name}, [(text, 0)]
    while pending:
        source, depth = pending.pop()
        for include in includes(source, ctx or ConditionContext()):
            if include in seen or depth >= _MAX_INCLUDE_DEPTH or not _SAFE_NAME.match(include):
                continue
            seen.add(include)
            try:
                extra = (path.parent / include).read_text(encoding="utf-8")
            except OSError:
                continue  # an include the folder does not have is skipped
            messages += parse_csv(extra)
            pending.append((extra, depth + 1))
    return messages


def _read_cached(path: Path) -> tuple[float, str] | None:
    try:
        return time.time() - path.stat().st_mtime, path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # unreadable or not UTF-8: as if not cached
        return None


def _write_cached(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as err:  # a read-only cache costs the next start a fetch
        _LOGGER.debug("cache %s not written: %s", path, err)
