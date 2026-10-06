# Library guide

Everything the CLI does is available from Python. Import from the package
root:

```python
from gst_validator import (
    GSTIN,
    GSTINLayout,
    Captcha,
    GSTClient,
    AsyncGSTClient,
    TaxpayerDetails,
    TaxpayerProfile,
    Address,
    Jurisdiction,
    GoodsOrService,
    FinancialYear,
    FilingPreference,
    validate_many,
    enrich_many,
    ValidationResult,
    TTLCache,
    DiskCache,
    NullCache,
    DEFAULT_CACHE,
    TaxpayerCache,
    GSTValidatorError,
    InvalidGSTINError,
    CaptchaError,
    TaxpayerLookupError,
)
```

Every public type is a frozen dataclass, so results are hashable, comparable
and safe to share between threads. The package ships `py.typed`, and is
checked under both mypy and pyright in strict mode.

Everything is importable from the package root:

```python
from gst_validator import (
    GSTIN,
    Captcha,
    GSTClient,
    AsyncGSTClient,
    TaxpayerDetails,
    TaxpayerProfile,
    Address,
    Jurisdiction,
    GoodsOrService,
    FinancialYear,
    FilingPreference,
    TTLCache,
    NullCache,
    DEFAULT_CACHE,
    TaxpayerCache,
    GSTValidatorError,
    InvalidGSTINError,
    CaptchaError,
    TaxpayerLookupError,
)
```

## 1. Validate a GSTIN (no network, no captcha)

```python
from gst_validator import GSTIN, InvalidGSTINError

GSTIN.is_valid("27AAACR5055K1Z7")  # True  - never raises
GSTIN.is_valid("27AAACR5055K1ZA")  # False - checksum digit is wrong

gstin = GSTIN.parse(" 27aaacr5055k1z7 ")  # strips, upper-cases, validates
gstin.value  # '27AAACR5055K1Z7'
gstin.state_code  # '27'
gstin.state_name  # 'Maharashtra'
gstin.pan  # 'AAACR5055K'
gstin.entity_type  # 'Company'   (4th PAN character)
gstin.registration_sequence  # '1'         (Nth registration of this PAN in this state)

try:
    GSTIN.parse(user_input)
except InvalidGSTINError as error:
    print(error.value, error.reason)  # the input, and why it was rejected
```

`GSTIN` is a frozen dataclass: hashable, comparable, usable as a dict key.
Take one as a function parameter and malformed input cannot reach your code.

## 2. The data you get without a captcha

Three portal endpoints return data with no captcha at all, verified against
the live portal with no cookies and no prior captcha solve. The client still
opens a session first, since the portal could tighten this at any time:

```python
from gst_validator import GSTClient

with GSTClient() as client:
    client.fetch_goods_and_services("27AAACR5055K1Z7")
    # (GoodsOrService(code='55151190', description='OTHER', is_service=False),
    #  GoodsOrService(code='39269080', description='POLYPROPYLENE ARTICLES, NOT
    #                 ELSEWHERE SPECIFIED OR INCLUDED', is_service=False), ...)
    # Service providers come back as SAC codes instead, with is_service=True:
    #   GoodsOrService(code='998314', description='Information technology
    #                  design and development services', is_service=True)

    client.fetch_financial_years("27AAACR5055K1Z7")
    # (FinancialYear(label='2025-2026', value='2025'), FinancialYear('2026-2027', '2026'))

    client.fetch_filing_preferences("27AAACR5055K1Z7")
    # (FilingPreference(quarter='Q1', preference='Q'), ...)   -> .is_quarterly / .is_monthly
```

## 3. Three layouts, one class

The portal issues more than one shape of GSTIN, and most of `GSTIN`'s
properties apply to only one of them, so `layout` is the value to branch on:

```python
from gst_validator import GSTIN, GSTINLayout

match GSTIN.parse(value).layout:
    case GSTINLayout.PAN:  # 27AAACR5055K1Z7, the ordinary one
        ...  # pan, entity_type, registration_type apply
    case GSTINLayout.NON_RESIDENT:  # 9917USA29016OS6, an overseas OIDAR provider
        ...  # holder_code is the country code
    case GSTINLayout.UIN:  # 2317UNO00001UND, a UN body or embassy
        ...  # holder_code is the body, state_name applies
```

Properties that do not apply to the layout in hand return `None` rather than
raising, and `is_regular`, `is_non_resident` and `is_uin` remain as shortcuts.

## 4. The full lookup (one captcha)

The captcha is bound to the client's cookies, so fetch and submit must happen
on the **same instance**:

```python
with GSTClient() as client:
    captcha = client.fetch_captcha()
    solved = input(f"solve this: {captcha.data_uri}\n> ")
    profile = client.fetch_profile("27AAACR5055K1Z7", solved)

profile.name  # registered trade name, else the legal name
profile.is_active  # True
profile.details  # TaxpayerDetails
profile.as_dict()  # everything, JSON-ready
```

`fetch_details()` instead of `fetch_profile()` if you only want the
captcha-gated part.

## 5. Web app: captcha to the browser, text back

The pattern the original Flask app was reaching for: keep one client per
pending lookup, keyed by a session id:

```python
import uuid
from fastapi import FastAPI, HTTPException
from gst_validator import GSTClient, GSTValidatorError, InvalidGSTINError

app = FastAPI()
pending: dict[str, GSTClient] = {}  # swap for Redis + a TTL in production


@app.post("/captcha")
def start() -> dict[str, str]:
    client = GSTClient()
    captcha = client.fetch_captcha()
    session_id = str(uuid.uuid4())
    pending[session_id] = client
    return {"session_id": session_id, "image": captcha.data_uri}


@app.post("/lookup")
def lookup(session_id: str, gstin: str, captcha: str) -> dict[str, object]:
    client = pending.pop(session_id, None)
    if client is None:
        raise HTTPException(400, "unknown or expired session")
    try:
        return client.fetch_profile(gstin, captcha).as_dict()
    except InvalidGSTINError as error:
        raise HTTPException(422, str(error)) from error
    except GSTValidatorError as error:
        raise HTTPException(502, str(error)) from error
    finally:
        client.close()
```

The front end renders `image` straight into `<img src="{{ image }}">`, since it is
already a `data:` URI. Give `pending` an expiry; portal sessions do not live
forever, and an abandoned entry leaks a connection pool.

## 6. Checking many at once

```python
from gst_validator import validate_many, enrich_many

for row in validate_many(["27AAACR5055K1Z7", "nope"]):
    print(row.value, row.is_valid, row.error)  # never raises

# the three captcha-free endpoints, over one shared session
for row in enrich_many(validate_many(gstins)):
    print(row.gstin, [str(c) for c in row.goods_and_services])
```

`validate_many` yields a `ValidationResult` per input: the value exactly as
given, the parsed `GSTIN` or `None`, and `error` when it would not parse. It
takes an `extras` iterable of per-row dicts to carry other columns through,
which is what the CLI uses for CSV. `as_dict()` is flat and has the same keys
for valid and invalid rows, so it drops straight into `csv.DictWriter` or a
dataframe.

`enrich_many` adds the captcha-free data over one session, recording a
per-row `enrichment_error` instead of failing the batch. The captcha-gated
lookup is deliberately not part of it, since each one costs a solved image.

## 7. Async

Same API, `await` and `async with`:

```python
import asyncio
from gst_validator import AsyncGSTClient


async def codes(gstin: str) -> tuple[str, ...]:
    async with AsyncGSTClient() as client:
        items = await client.fetch_goods_and_services(gstin)
        return tuple(item.code or "" for item in items)


asyncio.run(codes("27AAACR5055K1Z7"))
```

## 8. Caching

Each live lookup costs a human-solved captcha, so successful results are
cached in a process-wide `TTLCache` (24 h, 512 entries, LRU, thread-safe).

```python
from gst_validator import DEFAULT_CACHE, GSTClient, NullCache, TTLCache

GSTClient()  # shares DEFAULT_CACHE
GSTClient(cache=TTLCache(ttl=300))  # private, 5-minute cache
GSTClient(cache=NullCache())  # caching off

with GSTClient() as client:
    if (hit := client.cached(gstin)) is not None:
        details = hit  # no captcha spent
    else:
        details = client.fetch_details(gstin, solved)

    client.fetch_details(gstin, solved, refresh=True)  # bypass and overwrite
```

**The CLI caches to disk.** A command-line run is a fresh process every time,
so an in-memory cache would never hit: looking the same GSTIN up twice would
mean solving two captchas. Entries live in the platform cache directory
(`~/Library/Caches/gst-validator` on macOS,
`${XDG_CACHE_HOME:-~/.cache}/gst-validator` on Linux, `%LOCALAPPDATA%` on
Windows), one small JSON file per GSTIN, expiring after 24 hours.

Those files hold taxpayer data at rest, a registered name and place of
business, so they are worth knowing about:

```bash
gst-validator 27AAACR5055K1Z7 --no-cache      # neither read nor written
gst-validator --clear-cache                   # delete the lot
```

```python
from gst_validator import DiskCache

DiskCache.default_directory()  # where it keeps them
DiskCache(ttl=3600).clear()  # or manage it yourself
```

What is stored is the portal's own response body, so an entry written by an
older version still reads back after the models grow.

Back it with anything that satisfies the `TaxpayerCache` protocol:

```python
import json
from gst_validator import TaxpayerDetails


class RedisCache:
    def __init__(self, redis, ttl: int = 86_400) -> None:
        self._redis, self._ttl = redis, ttl

    def get(self, gstin: str) -> TaxpayerDetails | None:
        blob = self._redis.get(f"gst:{gstin}")
        return TaxpayerDetails.from_payload(json.loads(blob)) if blob else None

    def set(self, gstin: str, details: TaxpayerDetails) -> None:
        self._redis.setex(f"gst:{gstin}", self._ttl, json.dumps(details.raw))


client = GSTClient(cache=RedisCache(redis_connection))
```

**The client is deliberately not a singleton.** It owns the cookies a captcha
is bound to, so one shared instance would cross captcha sessions between
concurrent lookups. The *cache* is the shared piece; clients stay cheap and
short-lived. The cache stores `.raw`, so a cached entry survives a model
upgrade.

## 9. Error handling

```
GSTValidatorError
├── InvalidGSTINError   (also a ValueError)  .value, .reason
├── CaptchaError                             captcha could not be fetched
└── TaxpayerLookupError                      .code = the portal's errorCode
```

```python
from gst_validator import CaptchaError, GSTValidatorError, InvalidGSTINError, TaxpayerLookupError

try:
    profile = client.fetch_profile(gstin, solved)
except InvalidGSTINError:
    ...  # bad input, never hit the network
except CaptchaError:
    ...  # portal did not hand out an image
except TaxpayerLookupError as error:
    if error.code == "SWEB_9000":
        ...  # wrong or expired captcha - fetch a new one
except GSTValidatorError:
    ...  # catch-all for this package
```

The portal answers rejections with HTTP 200 and a body carrying an
`errorCode`, so the *absence* of `gstin` in the body, not the status code,
is what marks a failed lookup. One `except GSTValidatorError` catches
everything this package raises; `httpx` errors are wrapped, never leaked.

---


# What you get back

### `TaxpayerProfile`

| Attribute | Type | Source |
|---|---|---|
| `details` | `TaxpayerDetails` | `taxpayerDetails` (captcha) |
| `goods_and_services` | `tuple[GoodsOrService, ...]` | `goodservice` |
| `financial_years` | `tuple[FinancialYear, ...]` | `dropdownfinyear` |
| `filing_preferences` | `tuple[FilingPreference, ...]` | `taxpayerProfileDetails` |

Shortcuts: `gstin`, `name`, `is_active`, `as_dict()`.

### `TaxpayerDetails`

| Attribute | Portal key | Type |
|---|---|---|
| `gstin` / `number` | `gstin` | `str` / `GSTIN \| None` |
| `legal_name` | `lgnm` | `str \| None` |
| `trade_name` | `tradeNam` | `str \| None` |
| `name` | (derived) | trade name, else legal name |
| `status` | `sts` | `str \| None` ("Active", "Inactive", "Cancelled suo-moto") |
| `constitution` | `ctb` | `str \| None` |
| `taxpayer_type` | `dty` | `str \| None` |
| `registration_date` | `rgdt` | `datetime.date \| None` |
| `cancellation_date` | `cxdt` | `datetime.date \| None` |
| `last_updated` | `lstupdt` | `datetime.date \| None` (not seen from this endpoint) |
| `nature_of_business` | `nba` | `tuple[str, ...]` |
| `principal_address` | `pradr` | `Address \| None` |
| `additional_addresses` | `adadr` | `tuple[Address, ...]` (not seen from this endpoint) |
| `central_jurisdiction` | `ctj`, `ctjCd` | `Jurisdiction` |
| `state_jurisdiction` | `stj`, `stjCd` | `Jurisdiction` |
| `einvoice_enabled` | `einvoiceStatus` | `bool \| None` |
| `is_field_visit_conducted` | `isFieldVisitConducted` | `bool \| None` |
| `core_business_activity` | `ntcrbs` (`SPO` and `MFT` expanded; any other code passes through) | `str \| None` |
| `aadhaar_verified` | `adhrVFlag` | `bool \| None` |
| `aadhaar_verified_on` | `adhrVdt` | `datetime.date \| None` |
| `ekyc_status` | `ekycVFlag` | `str \| None` |
| `composition_rate` | `cmpRt` | `str \| None` (answered "NA" even for composition dealers) |
| `raw` | everything | `dict[str, Any]` |

`as_dict()` returns every field above under its own name, with dates as ISO
strings, plus the derived `is_active` and `is_cancelled` so a consumer can
tell "Inactive" from "Cancelled suo-moto" without parsing the status string,
and `extra` holding anything unmapped.

Helpers: `is_active`, `is_cancelled`, `addresses` (principal first),
`as_dict()`, and `unmapped`, which lists portal keys this class does not model, so a new
portal field is never silently dropped.

`Address` carries split fields (`building_name`, `street`, `pincode`, …) *and*
`full`: the portal usually sends the principal address as one `adr` string, so
`as_line()` returns whichever form arrived.

---
