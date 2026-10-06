"""Lookup cache.

Every portal lookup costs one human-solved captcha, so a cache hit is worth far
more here than in an ordinary HTTP client. The cache is process-wide and shared
between clients on purpose: the *client* must stay per-session (it owns the
cookies a captcha is bound to), while cached results are session-independent.
"""

import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable

from .models import TaxpayerDetails

__all__ = ["DEFAULT_CACHE", "DiskCache", "NullCache", "TTLCache", "TaxpayerCache"]

# A cache key becomes a file name, so it may hold nothing that could climb out
# of the directory. GSTINs are upper-case alphanumeric and fifteen characters.
_CACHE_KEY: Final = re.compile(r"^[0-9A-Z]{1,32}$")

_DEFAULT_TTL: Final = 24 * 60 * 60.0
_DEFAULT_MAXSIZE: Final = 512


@runtime_checkable
class TaxpayerCache(Protocol):
    """Minimal cache contract - swap in Redis, Django cache, whatever."""

    def get(self, gstin: str) -> TaxpayerDetails | None: ...

    def set(self, gstin: str, details: TaxpayerDetails) -> None: ...


class TTLCache:
    """Thread-safe, size-bounded, time-to-live cache with LRU eviction."""

    def __init__(
        self,
        *,
        ttl: float = _DEFAULT_TTL,
        maxsize: int = _DEFAULT_MAXSIZE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.ttl: float = ttl
        self.maxsize: int = maxsize
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[str, tuple[float, TaxpayerDetails]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, gstin: str) -> TaxpayerDetails | None:
        with self._lock:
            entry = self._entries.get(gstin)
            if entry is None:
                return None
            stored_at, details = entry
            if self._clock() - stored_at >= self.ttl:
                del self._entries[gstin]
                return None
            self._entries.move_to_end(gstin)
            return details

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        with self._lock:
            self._entries[gstin] = (self._clock(), details)
            self._entries.move_to_end(gstin)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, gstin: object) -> bool:
        return isinstance(gstin, str) and self.get(gstin) is not None


class NullCache:
    """Cache that never stores anything - pass it to disable caching."""

    def get(self, gstin: str) -> TaxpayerDetails | None:
        return None

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        return None


DEFAULT_CACHE: Final[TTLCache] = TTLCache()
"""Process-wide cache used by clients that are not given one explicitly."""


class DiskCache:
    """A cache that survives the process, for the command line.

    :class:`TTLCache` only helps a long-lived process: a CLI run starts with an
    empty one, so looking the same GSTIN up twice would cost two captchas. This
    keeps one small JSON file per GSTIN instead.

    The stored body is the portal's own payload, so an entry written by an
    older version still reads back once the models grow. **It is taxpayer data
    at rest**: a registered name and place of business sit in plain files under
    :meth:`default_directory`, which :meth:`clear` empties.
    """

    def __init__(self, *, ttl: float = _DEFAULT_TTL, directory: Path | None = None) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.ttl: float = ttl
        self.directory: Path = directory or self.default_directory()

    @staticmethod
    def default_directory() -> Path:
        """The platform's cache location, without pulling in a dependency."""
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches"
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        return base / "gst-validator"

    @staticmethod
    def _safe_key(gstin: str) -> str:
        """Reject a key that would name a file outside the cache directory.

        Keys reaching this class from the clients are always validated GSTINs,
        but :class:`DiskCache` is part of the public surface and someone may
        wire it to input of their own, where a key of ``"../../x"`` would
        otherwise write outside :attr:`directory`.
        """
        if not _CACHE_KEY.fullmatch(gstin):
            raise ValueError(f"unsafe cache key {gstin!r}")
        return gstin

    def _path(self, gstin: str) -> Path:
        return self.directory / f"{self._safe_key(gstin)}.json"

    def get(self, gstin: str) -> TaxpayerDetails | None:
        path = self._path(gstin)
        try:
            body: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing, unreadable or corrupt: a cache miss, never an error.
            return None
        if not isinstance(body, dict):
            return None
        entry = cast(dict[str, Any], body)
        stored_at = entry.get("stored_at")
        payload = entry.get("payload")
        if not isinstance(stored_at, int | float) or not isinstance(payload, dict):
            return None
        if time.time() - float(stored_at) >= self.ttl:
            path.unlink(missing_ok=True)
            return None
        return TaxpayerDetails.from_payload(cast(dict[str, Any], payload))

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        # Outside the try on purpose: an unsafe key is a mistake in the calling
        # code and must be heard, not swallowed with the write failures below.
        path = self._path(gstin)
        entry = {"stored_at": time.time(), "payload": details.raw}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a crash cannot leave half a file behind.
            # The temporary name carries the process id, so two runs caching
            # the same GSTIN at once cannot write to one another's file before
            # the rename makes it visible.
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(entry), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError):
            # A cache that cannot be written is not a reason to fail a lookup,
            # whether the directory is unwritable or the payload will not
            # serialise.
            return

    def clear(self) -> int:
        """Delete every cached entry and report how many were removed."""
        removed = 0
        # Also sweeps temporary files a run that died mid-write left behind.
        for path in (*self.directory.glob("*.json"), *self.directory.glob("*.tmp")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
