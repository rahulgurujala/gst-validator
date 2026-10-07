# Running this behind a web service

> **Read this part first.**
>
> The GST portal is a public service, paid for out of public money, and it
> was built for people looking things up one at a time. It was not built to
> be mined, and this package is not a scraper. Use it to check a supplier
> before you invoice them, or a notice before you act on it. Do not use it to
> pull down a copy of the register.
>
> If you run it in a loop over GSTINs you have no business looking at, you
> will get the address blocked, and you will take out every other service
> and colleague sharing it. That is your problem to fix, not ours. The
> licence gives you no warranty and the authors no liability; nobody here can
> get you unblocked, and nobody here will ask the department to on your
> behalf.
>
> Concretely, do not: enumerate GSTINs, rebuild the taxpayer register,
> harvest the practitioner directory for contact details, point an automated
> solver at the captcha, or rotate addresses to get around a block. If your
> volume needs any of that, you need the official
> [GST API](https://developer.gst.gov.in/) through a licensed GSP, which is
> what it is for.

Everything else in these guides assumes one person at a terminal. A web
service is different: every one of your users' requests leaves from **one
address**, and the portal counts addresses.

This page is about not getting that address blocked.

## The risk, plainly

The GST portal sits behind a firewall. Past some rate it stops answering an
address entirely - not the one endpoint, **everything**, including the
captcha-free searches. It answers HTTP 200 with an HTML page reading
`Request Rejected` and a support ID.

This is easy to trigger by accident. It happened while developing this
package: a test run made several dozen calls in quick succession and every
endpoint went dark, including ones that had answered seconds earlier. It
cleared on its own after a pause.

Three things follow:

- **It is per address, not per session.** A new client, a new process or a
  fresh cookie jar will not help. Nor will restarting your service.
- **It takes everybody down with it.** One careless loop blocks the lookups
  your users are waiting on.
- **It is waited out, not worked around.** Changing address to get past it is
  evading the control rather than respecting it, and it fails as soon as the
  new address is counted too. If you genuinely need unattended volume, the
  official [GST API](https://developer.gst.gov.in/) through a licensed GSP is
  the supported route and exists for exactly this.

## What the package does for you

Out of the box, every client:

- **paces itself** to one request per second,
- **collapses** concurrent identical captcha-free requests into one,
- **stops sending** for five minutes once it sees a block, rather than
  hammering a door that is already shut.

That is enough for a command line. It is **not** enough for a web service,
for one reason: those defaults are per process.

## The one thing you must change

`IntervalLimiter` lives on a client object. Four Uvicorn workers each holding
one emit **four requests a second**, not one. Build a limiter over something
shared and hand it to every client:

```python
from gst_validator import GSTClient, RateLimiter


class RedisLimiter:
    """One budget for every worker. Sketch: use a real token bucket."""

    def __init__(self, redis, rate: float = 1.0) -> None:
        self._redis, self._rate = redis, rate

    def acquire(self) -> None: ...  # block until the shared bucket yields a token

    async def acquire_async(self) -> None: ...


limiter: RateLimiter = RedisLimiter(redis)
client = GSTClient(limiter=limiter)
```

Share the circuit breaker the same way, so that when one worker discovers the
block, the others stop too:

```python
from gst_validator import CircuitBreaker

BREAKER = CircuitBreaker(cool_off=300)  # module level, one per process
GSTClient(limiter=limiter, breaker=BREAKER)
```

## Cache, and the problem mostly goes away

A registered name and address change rarely. With a shared cache, a thousand
users looking up the same GSTIN is **one** call to the portal:

```python
from gst_validator import GSTClient, TaxpayerCache, TaxpayerDetails


class RedisCache:
    def __init__(self, redis, ttl: int = 86_400) -> None:
        self._redis, self._ttl = redis, ttl

    def get(self, gstin: str) -> TaxpayerDetails | None:
        blob = self._redis.get(f"gst:{gstin}")
        return TaxpayerDetails.from_payload(json.loads(blob)) if blob else None

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        self._redis.setex(f"gst:{gstin}", self._ttl, json.dumps(details.raw))


cache: TaxpayerCache = RedisCache(redis)
```

Of everything on this page, this is the highest-value change. Traffic to the
portal becomes roughly *distinct GSTINs per day* rather than *requests per
second*.

## Let your users solve the captchas

Four of the eleven searches need a human to read an image, and that is a
feature here rather than an obstacle: it caps those endpoints at human speed
no matter how many users you have. Hand the image to the browser and take the
answer back:

```python
captcha = client.fetch_captcha()
return {"image": captcha.data_uri}  # straight into <img src="...">
```

See [the recipes](recipes.md) for the full request/response pair. Keep one
client per pending lookup: the captcha is bound to the session that fetched
it.

Your real exposure is the **captcha-free** endpoints - HSN search, the
practitioner directory, and the three enrichment lookups. Those are the ones
a burst of users can hammer, and the ones the limiter and cache must cover.

## Handling a block in a request handler

`PortalBlockedError` means "we know we are blocked and are not sending". It is
distinct from the portal rejecting a request you just made, so you can answer
from cache, queue the work, or return 503 with a sensible `Retry-After`:

```python
from gst_validator import PortalBlockedError

try:
    profile = client.fetch_profile(gstin, solved)
except PortalBlockedError as error:
    raise HTTPException(
        503,
        "upstream temporarily unavailable",
        headers={"Retry-After": str(int(error.seconds_remaining))},
    )
```

## A worked example

Everything above, in one FastAPI app. The pieces that matter are at the top:
one limiter, one breaker and one cache for the whole process, not one per
request.

```python
"""A GSTIN lookup service. Run with: uvicorn app:api"""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request

from gst_validator import (
    AsyncGSTClient,
    CircuitBreaker,
    GSTValidatorError,
    GSTIN,
    InvalidGSTINError,
    PortalBlockedError,
    TaxpayerDetails,
)

REDIS = aioredis.from_url("redis://localhost")


# One budget for every worker. A per-client limiter would give each worker
# its own, which is the mistake this whole page is about.
class SharedLimiter:
    """A token bucket in Redis. One request per second across the fleet."""

    def __init__(self, redis: Any, rate: float = 1.0) -> None:
        self._redis, self._rate = redis, rate

    async def acquire_async(self) -> None:
        while True:
            now = time.time()
            # INCR the slot for this second; the first caller wins it.
            slot = int(now / self._rate)
            count = await self._redis.incr(f"gst:slot:{slot}")
            if count == 1:
                await self._redis.expire(f"gst:slot:{slot}", 5)
                return
            await asyncio.sleep(self._rate / 4)

    def acquire(self) -> None:  # the sync half of the protocol
        raise NotImplementedError("this service is async only")


class SharedCache:
    """Taxpayer details change rarely, so this is the biggest lever there is."""

    def __init__(self, redis: Any, ttl: int = 86_400) -> None:
        self._redis, self._ttl = redis, ttl

    def get(self, gstin: str) -> TaxpayerDetails | None:
        blob = self._redis.get(f"gst:tp:{gstin}")
        return TaxpayerDetails.from_payload(json.loads(blob)) if blob else None

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        self._redis.setex(f"gst:tp:{gstin}", self._ttl, json.dumps(details.raw))


# Module level: shared by every request this worker serves.
BREAKER = CircuitBreaker(cool_off=300)
LIMITER = SharedLimiter(REDIS)
CACHE = SharedCache(REDIS)

# A captcha is bound to the client that fetched it, so a pending lookup has
# to hold on to its own client. Give these a TTL in anything real.
PENDING: dict[str, AsyncGSTClient] = {}


def new_client() -> AsyncGSTClient:
    return AsyncGSTClient(limiter=LIMITER, breaker=BREAKER, cache=CACHE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for client in PENDING.values():
        await client.aclose()


api = FastAPI(lifespan=lifespan)


@api.exception_handler(PortalBlockedError)
async def blocked(request: Request, error: PortalBlockedError):
    """We know we are blocked and are not sending. Say so honestly."""
    raise HTTPException(
        503,
        "the GST portal is temporarily refusing requests from this service",
        headers={"Retry-After": str(int(error.seconds_remaining))},
    )


@api.get("/validate/{gstin}")
async def validate(gstin: str) -> dict[str, Any]:
    """Free: no network at all. Screen here before spending anything."""
    try:
        number = GSTIN.parse(gstin)
    except InvalidGSTINError as error:
        raise HTTPException(422, error.reason) from error
    return {
        "gstin": number.value,
        "state": number.state_name,
        "pan": number.pan,
        "entity_type": number.entity_type,
    }


@api.get("/codes/{gstin}")
async def codes(gstin: str) -> list[str]:
    """Captcha-free, but it does reach the portal, so it is paced and
    coalesced: a hundred users asking at once make one request."""
    client = new_client()
    try:
        return [str(item) for item in await client.fetch_goods_and_services(gstin)]
    except InvalidGSTINError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        await client.aclose()


@api.post("/lookup/start")
async def start_lookup(gstin: str) -> dict[str, str]:
    """Hand the captcha to the browser. The user solves it, not us.

    This is what keeps the gated endpoints at human speed however many
    users arrive, and it is the reason this design scales at all.
    """
    GSTIN.parse(gstin)  # reject rubbish before fetching anything
    client = new_client()
    captcha = await client.fetch_captcha()
    token = str(uuid.uuid4())
    PENDING[token] = client
    return {"token": token, "image": captcha.data_uri}


@api.post("/lookup/finish")
async def finish_lookup(token: str, gstin: str, captcha: str) -> dict[str, Any]:
    client = PENDING.pop(token, None)
    if client is None:
        raise HTTPException(400, "unknown or expired lookup")
    try:
        profile = await client.fetch_profile(gstin, captcha)
        return profile.as_dict()
    except GSTValidatorError as error:
        raise HTTPException(502, str(error)) from error
    finally:
        await client.aclose()
```

Two things to add before this is production-ready, both outside the scope of
this package: a rate limit of your own in front, so one user cannot spend the
whole budget, and a real store for `PENDING` with an expiry, since an
abandoned lookup holds a connection pool open.

## A checklist

- [ ] A shared `RateLimiter`, not the per-process default
- [ ] A shared `CircuitBreaker`
- [ ] A shared `TaxpayerCache` with a real TTL
- [ ] Users solve their own captchas
- [ ] `PortalBlockedError` handled as a 503, not a 500
- [ ] Your own rate limit in front, so one user cannot spend everyone's budget
- [ ] Batch work off the request path, and well under your portal budget

If you outgrow this, that is a signal rather than an obstacle: a licensed GSP
is what the portal provides for volume, and no amount of client-side care
substitutes for it.
