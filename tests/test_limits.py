"""Staying inside what the portal tolerates: pacing, coalescing, backing off."""

import asyncio
import threading
import time

import httpx
import pytest

from gst_validator import (
    AsyncGSTClient,
    CircuitBreaker,
    GSTClient,
    GSTValidatorError,
    IntervalLimiter,
    NullLimiter,
    PortalBlockedError,
    RateLimiter,
)
from gst_validator.limits import AsyncSingleFlight, SingleFlight

from .support import VALID_GSTIN, transport

BLOCK_PAGE = (
    "<html><head><title>Request Rejected</title></head><body>The requested URL "
    "was rejected. Please consult with your administrator.</body></html>"
)


class TestIntervalLimiter:
    def test_it_spaces_requests(self) -> None:
        limiter = IntervalLimiter(0.05)
        start = time.monotonic()
        for _ in range(4):
            limiter.acquire()
        # the first is free, three more wait one interval each
        assert time.monotonic() - start >= 0.14

    def test_zero_never_waits(self) -> None:
        limiter = IntervalLimiter(0)
        start = time.monotonic()
        for _ in range(100):
            limiter.acquire()
        assert time.monotonic() - start < 0.05

    def test_negative_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            IntervalLimiter(-1)

    def test_threads_share_one_budget(self) -> None:
        """Otherwise concurrency multiplies the rate, which is the whole risk."""
        limiter = IntervalLimiter(0.02)
        start = time.monotonic()
        threads = [threading.Thread(target=limiter.acquire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert time.monotonic() - start >= 0.17

    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(IntervalLimiter(0), RateLimiter)
        assert isinstance(NullLimiter(), RateLimiter)

    def test_async_waits_too(self) -> None:
        async def go() -> float:
            limiter = IntervalLimiter(0.05)
            start = time.monotonic()
            await limiter.acquire_async()
            await limiter.acquire_async()
            return time.monotonic() - start

        assert asyncio.run(go()) >= 0.04


class TestSingleFlight:
    def test_concurrent_identical_calls_become_one(self) -> None:
        calls = 0
        flight = SingleFlight()

        def work() -> str:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return "value"

        results: list[str] = []
        threads = [
            threading.Thread(target=lambda: results.append(flight.run("k", work)))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls == 1
        assert results == ["value"] * 20

    def test_different_keys_do_not_share(self) -> None:
        calls = 0
        flight = SingleFlight()

        def work() -> int:
            nonlocal calls
            calls += 1
            return calls

        flight.run("a", work)
        flight.run("b", work)
        assert calls == 2

    def test_a_failure_reaches_every_waiter(self) -> None:
        flight = SingleFlight()
        errors: list[str] = []

        def boom() -> str:
            time.sleep(0.03)
            raise ValueError("nope")

        def attempt() -> None:
            try:
                flight.run("k", boom)
            except ValueError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == ["nope"] * 5

    def test_a_later_call_runs_again(self) -> None:
        """Nothing is retained after the flight: caching is the cache's job."""
        calls = 0
        flight = SingleFlight()

        def work() -> int:
            nonlocal calls
            calls += 1
            return calls

        assert flight.run("k", work) == 1
        assert flight.run("k", work) == 2

    def test_async_coalesces_too(self) -> None:
        async def go() -> tuple[int, list[str]]:
            calls = 0
            flight = AsyncSingleFlight()

            async def work() -> str:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.05)
                return "value"

            out = await asyncio.gather(*(flight.run("k", work) for _ in range(20)))
            return calls, list(out)

        calls, results = asyncio.run(go())
        assert calls == 1
        assert results == ["value"] * 20


class TestCircuitBreaker:
    def test_it_opens_on_a_block(self) -> None:
        breaker = CircuitBreaker(cool_off=60)
        assert not breaker.is_open
        breaker.record_block()
        assert breaker.is_open

    def test_nothing_goes_out_during_the_cool_off(self) -> None:
        breaker = CircuitBreaker(cool_off=60)
        breaker.record_block()
        for _ in range(3):
            with pytest.raises(PortalBlockedError, match="retrying in"):
                breaker.before_request()

    def test_success_closes_it(self) -> None:
        breaker = CircuitBreaker(cool_off=60)
        breaker.record_block()
        breaker.record_success()
        assert not breaker.is_open
        breaker.before_request()  # must not raise

    def test_the_cool_off_expires(self) -> None:
        now = [1000.0]
        breaker = CircuitBreaker(cool_off=30, clock=lambda: now[0])
        breaker.record_block()
        assert breaker.is_open
        now[0] += 31
        assert not breaker.is_open
        breaker.before_request()

    def test_the_error_reports_the_wait(self) -> None:
        breaker = CircuitBreaker(cool_off=120)
        breaker.record_block()
        with pytest.raises(PortalBlockedError) as caught:
            breaker.before_request()
        assert caught.value.seconds_remaining > 100

    def test_negative_cool_off_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            CircuitBreaker(cool_off=-1)


class TestClientIntegration:
    @staticmethod
    def _blocked_transport() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=BLOCK_PAGE, headers={"content-type": "text/html"})

        return httpx.MockTransport(handler)

    def test_a_block_trips_the_breaker_and_stops_further_sends(self) -> None:
        """Sending into a block is pointless and plausibly prolongs it."""
        sent = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sent
            sent += 1
            return httpx.Response(200, text=BLOCK_PAGE, headers={"content-type": "text/html"})

        client = GSTClient(transport=httpx.MockTransport(handler), min_interval=0)
        with client:
            with pytest.raises(GSTValidatorError):
                client.fetch_goods_and_services(VALID_GSTIN)
            first = sent
            # the cool-off is running: nothing further should leave
            for _ in range(5):
                with pytest.raises(PortalBlockedError):
                    client.fetch_goods_and_services(VALID_GSTIN)
        assert sent == first, f"kept sending into the block: {sent} requests"

    def test_a_shared_breaker_protects_every_client(self) -> None:
        breaker = CircuitBreaker(cool_off=60)
        breaker.record_block()
        with GSTClient(transport=transport(), breaker=breaker, min_interval=0) as client:
            with pytest.raises(PortalBlockedError):
                client.fetch_goods_and_services(VALID_GSTIN)

    def test_a_custom_limiter_is_used(self) -> None:
        calls = 0

        class Counting:
            def acquire(self) -> None:
                nonlocal calls
                calls += 1

            async def acquire_async(self) -> None:
                nonlocal calls
                calls += 1

        with GSTClient(transport=transport(), limiter=Counting()) as client:
            client.fetch_goods_and_services(VALID_GSTIN)
        assert calls >= 1

    def test_concurrent_identical_lookups_hit_the_portal_once(self) -> None:
        """The FastAPI case: many users, the same GSTIN, one outbound call."""
        hits = 0
        lock = threading.Lock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hits
            if request.url.path.endswith("goodservice"):
                with lock:
                    hits += 1
                time.sleep(0.05)
                return httpx.Response(200, json={"bzsdtls": []})
            return httpx.Response(200, text="<html></html>")

        with GSTClient(transport=httpx.MockTransport(handler), min_interval=0) as client:
            threads = [
                threading.Thread(target=lambda: client.fetch_goods_and_services(VALID_GSTIN))
                for _ in range(15)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert hits == 1, f"{hits} requests where one would do"

    def test_the_async_client_coalesces_too(self) -> None:
        """The handler must actually await.

        A MockTransport that returns immediately never yields to the event
        loop, so the tasks run to completion one after another and there is
        nothing concurrent to collapse. Real network latency always yields,
        so the sleep here is what makes the test resemble production rather
        than a quirk of the mock.
        """
        hits = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hits
            if request.url.path.endswith("goodservice"):
                hits += 1
                await asyncio.sleep(0.05)
                return httpx.Response(200, json={"bzsdtls": []})
            await asyncio.sleep(0)
            return httpx.Response(200, text="<html></html>")

        async def go() -> None:
            async with AsyncGSTClient(
                transport=httpx.MockTransport(handler), min_interval=0
            ) as client:
                await asyncio.gather(
                    *(client.fetch_goods_and_services(VALID_GSTIN) for _ in range(15))
                )

        asyncio.run(go())
        assert hits == 1
