"""Lookup cache.

Every portal lookup costs one human-solved captcha, so a cache hit is worth far
more here than in an ordinary HTTP client. The cache is process-wide and shared
between clients on purpose: the *client* must stay per-session (it owns the
cookies a captcha is bound to), while cached results are session-independent.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final, Protocol, runtime_checkable

from .models import TaxpayerDetails

__all__ = ["DEFAULT_CACHE", "NullCache", "TTLCache", "TaxpayerCache"]

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
