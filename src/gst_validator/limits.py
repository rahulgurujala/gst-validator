"""Keeping a deployment inside what the portal tolerates.

The portal sits behind a firewall that blocks a whole address once it sees a
burst, and it does not spare the captcha-free endpoints. A command line run by
a person never gets near that. A web service fronting the same portal does,
because every one of its users' requests leaves from one address.

Nothing here disguises traffic. Each piece reduces it:

- :class:`IntervalLimiter` spaces requests out.
- :class:`SingleFlight` collapses identical concurrent requests into one.
- :class:`CircuitBreaker` stops sending once a block has started, which is
  both pointless and likely to prolong it.

For more than one process, implement :class:`RateLimiter` over something
shared, such as Redis. A per-process limiter in four workers is four times
the rate you think you set.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol, runtime_checkable

from .exceptions import GSTValidatorError

__all__ = [
    "AsyncSingleFlight",
    "CircuitBreaker",
    "IntervalLimiter",
    "NullLimiter",
    "PortalBlockedError",
    "RateLimiter",
    "SingleFlight",
]

DEFAULT_MIN_INTERVAL: Final = 1.0
"""Seconds between requests by default: far below a person browsing the site."""

DEFAULT_COOL_OFF: Final = 300.0
"""Seconds to stop sending after a block. The portal clears it on its own."""


class PortalBlockedError(GSTValidatorError):
    """Raised instead of sending, while the circuit is open.

    Distinct from the error the portal itself returns, so a caller can tell
    "we have been blocked and are waiting" from "we just tried and were
    rejected" and, for instance, answer a web request from cache instead.
    """

    def __init__(self, seconds_remaining: float) -> None:
        super().__init__(
            f"not sending: the portal blocked this address, retrying in {seconds_remaining:.0f}s"
        )
        self.seconds_remaining: float = seconds_remaining


@runtime_checkable
class RateLimiter(Protocol):
    """Minimal contract: block until a request may be sent.

    Implement over Redis, or any shared store, to give every worker in a
    deployment one budget rather than one each.
    """

    def acquire(self) -> None: ...

    async def acquire_async(self) -> None: ...


class IntervalLimiter:
    """Keeps a minimum gap between requests. Thread-safe, per process.

    This is the default, and it is only enough for a single process. Four
    Uvicorn workers each holding one emit four times the rate.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must not be negative")
        self.min_interval: float = min_interval
        self._next_free: float = 0.0
        self._lock = threading.Lock()

    def _claim(self) -> float:
        """Take the next slot and report how long to wait for it."""
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_free)
            self._next_free = start + self.min_interval
            return start - now

    def acquire(self) -> None:
        delay = self._claim()
        if delay:
            time.sleep(delay)

    async def acquire_async(self) -> None:
        delay = self._claim()
        if delay:
            await asyncio.sleep(delay)


class NullLimiter:
    """Limiter that never waits. For tests, and nothing else."""

    def acquire(self) -> None:
        return None

    async def acquire_async(self) -> None:
        return None


class CircuitBreaker:
    """Stops sending once the portal has blocked this address.

    Continuing to send into a block achieves nothing and plausibly extends
    it. After a block, calls fail immediately with
    :class:`PortalBlockedError` until the cool-off has passed; the first call
    after that goes out normally, and if it is blocked too the cool-off
    starts again.

    Share one between clients, and every one of them backs off together.
    """

    def __init__(
        self, cool_off: float = DEFAULT_COOL_OFF, clock: Callable[[], float] | None = None
    ) -> None:
        if cool_off < 0:
            raise ValueError("cool_off must not be negative")
        self.cool_off: float = cool_off
        self._clock = clock or time.monotonic
        self._blocked_until: float = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """Whether requests are currently being refused."""
        with self._lock:
            return self._clock() < self._blocked_until

    def before_request(self) -> None:
        """Raise :class:`PortalBlockedError` while the cool-off is running."""
        with self._lock:
            remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise PortalBlockedError(remaining)

    def record_block(self) -> None:
        """Note that the portal refused us, starting or restarting the cool-off."""
        with self._lock:
            self._blocked_until = self._clock() + self.cool_off

    def record_success(self) -> None:
        """Note a request that got through, closing the circuit."""
        with self._lock:
            self._blocked_until = 0.0


class _Call[T]:
    """One in-flight call, held by its leader and by everyone waiting on it."""

    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: T | None = None
        self.error: BaseException | None = None


class SingleFlight:
    """Collapses concurrent identical requests into one call.

    A hundred callers asking for the same GSTIN at the same moment is a
    hundred requests, because a cache only helps once the first has finished.
    Here the first caller does the work and the rest wait on its result, so
    the portal sees one.

    Keyed by whatever the caller passes, usually the endpoint and its
    parameters. Only calls in flight at the same moment share a result;
    retaining it afterwards is the cache's job, not this one's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, _Call[object]] = {}

    def run[T](self, key: str, work: Callable[[], T]) -> T:
        """Run ``work`` under ``key``, or wait on the call already running."""
        with self._lock:
            existing = self._calls.get(key)
            if existing is None:
                call: _Call[object] = _Call()
                self._calls[key] = call
                leader = True
            else:
                call, leader = existing, False

        if not leader:
            # Followers hold the record itself, so the leader clearing the
            # dictionary cannot lose the result from under them.
            call.done.wait()
            if call.error is not None:
                raise call.error
            return call.value  # type: ignore[return-value]

        try:
            call.value = work()
        except BaseException as error:
            call.error = error
            raise
        finally:
            with self._lock:
                self._calls.pop(key, None)
            call.done.set()
        return call.value


class _AsyncCall[T]:
    """One in-flight async call, held by its leader and by its waiters."""

    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.value: T | None = None
        self.error: BaseException | None = None


class AsyncSingleFlight:
    """:class:`SingleFlight` for the async client.

    Separate because a coroutine waits on an :class:`asyncio.Event` without
    blocking the loop, and on a threading one it would block everything.
    """

    def __init__(self) -> None:
        self._calls: dict[str, _AsyncCall[object]] = {}

    async def run[T](self, key: str, work: Callable[[], Awaitable[T]]) -> T:
        """Await ``work`` under ``key``, or wait on the call already running."""
        existing = self._calls.get(key)
        if existing is not None:
            # Holding the record, not the key: the leader clearing the
            # dictionary cannot take the result away before this reads it,
            # which it does, because set() does not yield to the waiters.
            await existing.done.wait()
            if existing.error is not None:
                raise existing.error
            return existing.value  # type: ignore[return-value]

        # No await between the lookup above and the claim below, and the loop
        # is single-threaded, so no lock is needed here.
        call: _AsyncCall[object] = _AsyncCall()
        self._calls[key] = call
        try:
            call.value = await work()
        except BaseException as error:
            call.error = error
            raise
        finally:
            self._calls.pop(key, None)
            call.done.set()
        return call.value
