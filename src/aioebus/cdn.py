"""The ebusd-configuration files on the public ebus.github.io CDN, kept in a
cache directory.

Network access goes through a ``fetch`` callable the caller provides (so an
application can use its own HTTP session); disk access runs in a thread. A
cached file is used while it is fresh, and a stale one stands in while the CDN
cannot be reached. Only real ebusd CSVs and folder listings are cached.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

CDN: Final = "https://ebus.github.io"
CACHE_MAX_AGE: Final = 7 * 24 * 3600  # s; older cached files are fetched again
_LISTING: Final = ".listing"  # cache name of a folder listing (no CDN name looks so)

# returns the text at a URL; "" when the server says it is not there, None when
# it cannot be reached (only then is a later try worth it)
Fetch = Callable[[str], Awaitable[str | None]]


class CdnFiles:
    """CDN files by path below ``/<lang>/`` (``vaillant/15.ctlv3.csv``; a path
    ending in '/' is a folder listing)."""

    def __init__(self, *, cache_dir: Path | None, fetch: Fetch | None, lang: str) -> None:
        self.cache_dir = cache_dir
        self.lang = lang
        self._fetch = fetch
        self._listings: dict[str, list[str] | None] = {}

    @property
    def usable(self) -> bool:
        """Whether files can come from anywhere (a fetch or a cache)."""
        return self._fetch is not None or self.cache_dir is not None

    def forget_listings(self) -> None:
        """Ask the CDN for folder listings again (once per catalog load)."""
        self._listings = {}

    async def listing(self, folder: str) -> list[str] | None:
        """A folder's file names, asked for once until :meth:`forget_listings`
        (an unreachable CDN then costs one timeout, not one per device)."""
        if folder not in self._listings:
            text = await self.text(f"{folder}/")
            self._listings[folder] = text.split("\n") if text else None
        return self._listings[folder]

    async def text(self, rel: str, lang: str | None = None) -> str | None:
        """A file (a folder listing for a path ending in '/'), from the cache
        while it is fresh, else fetched (and cached); a stale copy stands in when
        the CDN cannot be reached or answers with something else. "" when the
        CDN says it has no such file (and nothing is cached)."""
        return (await self.text_state(rel, lang))[0]

    async def text_state(self, rel: str, lang: str | None = None) -> tuple[str | None, bool]:
        """:meth:`text` and whether that is the current state (a fresh copy, a
        fetched file, or the CDN's final "not there") rather than a stand-in."""
        lang = lang or self.lang
        cached = None
        if self.cache_dir is not None:
            cached = self.cache_dir / lang / (rel + _LISTING if rel.endswith("/") else rel)
        stale: str | None = None
        if cached is not None:
            age_text = await asyncio.to_thread(_read_cached, cached)
            if age_text is not None:
                age, stale = age_text
                if age < CACHE_MAX_AGE:
                    return stale, True
        if self._fetch is None:
            return stale, False
        try:
            text = await self._fetch(f"{CDN}/{lang}/{rel}")
        except Exception as err:  # a fetch that raises counts as unreachable
            _LOGGER.debug("%s not fetched: %s", rel, err)
            text = None
        if not text:  # None: not reachable; "": not there (final)
            return (stale if stale is not None else text), text == ""
        if rel.endswith("/"):  # a folder listing is a JSON string of names
            try:
                listing = json.loads(text)
            except ValueError:
                listing = None
            if not isinstance(listing, str):
                return stale, False
            text = listing
        elif not text.lstrip().startswith("type,"):
            # not an ebusd CSV (e.g. a captive portal's page): never cache that
            _LOGGER.warning("%s: unexpected content, ignored", rel)
            return stale, False
        if cached is not None:
            await asyncio.to_thread(_write_cached, cached, text)
        return text, True


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
