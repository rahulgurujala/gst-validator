# Running this behind a web service

Everything else in these guides assumes one person at a terminal. A web
service is different: every one of your users' requests leaves from **one
address**, and the portal counts addresses.

This page is about not getting that address blocked.

## The risk, plainly

The GST portal sits behind a firewall. Past some rate it stops answering an
address entirely — not the one endpoint, **everything**, including the
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

Your real exposure is the **captcha-free** endpoints — HSN search, the
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
