# gst-validator

Validate Indian GSTINs offline and pull taxpayer details from the public GST
portal. Ships as a typed library (`import gst_validator`) and a CLI
(`gst-validator`). The CLI is a thin wrapper over the same public API, so
anything it does, your app can do.

- Offline GSTIN validation: format **and** mod-36 checksum, no network
- Structured objects, not raw dicts: dates parsed, `"NA"`/`""` normalised to `None`
- Captcha as bytes / base64 / data URI, so a browser or a human can solve it
- Sync and async clients, strict-typed, `py.typed`
- Built-in TTL cache, because each lookup costs one human-solved captcha
- Three extra portal endpoints that need **no captcha** at all

## Install

```bash
uv add gst-validator          # into your project
uv sync                       # working on this repo
```

Requires Python 3.13+. Only runtime dependency: `httpx`.

---

# CLI

```
gst-validator [-h] [--offline] [--json] [--details-only] [--raw]
              [--captcha-path PATH] [--captcha-base64] [--refresh]
              [--keep-captcha] GSTIN
```

### Validate without touching the network

```bash
$ gst-validator 27AAACR5055K1Z7 --offline
27AAACR5055K1Z7 is valid (state 27, PAN AAACR5055K)

$ gst-validator 27AAACR5055K1Z7 --offline --json
{"gstin": "27AAACR5055K1Z7", "state_code": "27", "pan": "AAACR5055K"}
```

Use this in CI, in a pre-commit check, or to screen input before spending a
captcha. Exit code `2` means the GSTIN is malformed.

### Full lookup (interactive)

```bash
$ gst-validator 27AAACR5055K1Z7
captcha image written to /tmp/27AAACR5055K1Z7-captcha.png   # stderr
captcha text: 784077
gstin                  27AAACR5055K1Z7
legal_name             <registered name as the portal returns it>
status                 Active
principal_address      <registered place of business, one line>
goods_and_services     39269080 - POLYPROPYLENE ARTICLES, NOT ELSEWHERE SPECIFIED...
financial_years        2017-2018, 2018-2019, ...
...
```

Open the image, type the text. The file is deleted once you have entered it.

### Machine-readable output

```bash
gst-validator 27AAACR5055K1Z7 --json     # modelled fields, dates as ISO strings
gst-validator 27AAACR5055K1Z7 --raw      # the portal's body verbatim, nothing dropped
```

`--json` is the one to parse: stable key names, `null` instead of `"NA"`,
dates as `2025-09-15`. `--raw` is for debugging what the portal actually sent.
Both go to stdout; progress messages go to stderr, so piping is safe:

```bash
gst-validator 27AAACR5055K1Z7 --json | jq -r '.legal_name, .principal_address'
```

### Solving the captcha somewhere else

```bash
$ gst-validator 27AAACR5055K1Z7 --captcha-base64
data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALY...
captcha text: 784077
```

The data URI goes to stdout. Paste it into a browser address bar, drop it in
an `<img src=...>`, or hand it to a solving service. The process keeps the
portal session open while it waits on stdin, which is what makes this work.

### Other flags

```bash
gst-validator 27AAACR5055K1Z7 --details-only   # skip the captcha-free extras
gst-validator 27AAACR5055K1Z7 --refresh        # ignore the cache, force a fresh lookup
gst-validator 27AAACR5055K1Z7 --keep-captcha   # keep the image file for inspection
gst-validator 27AAACR5055K1Z7 --captcha-path ./c.png   # write it where you want
```

Also runnable as a module: `python -m gst_validator 27AAACR5055K1Z7`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | lookup failed (wrong captcha, portal error, network) |
| `2` | GSTIN failed format or checksum validation |
| `130` | aborted (Ctrl-C / EOF) |

```bash
if gst-validator "$GSTIN" --offline >/dev/null 2>&1; then
  echo "well-formed"
fi
```

---

# Using it in your app

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

gstin = GSTIN.parse(" 27aatcm7522p1zj ")  # strips, upper-cases, validates
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

## 3. The full lookup (one captcha)

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

## 4. Web app: captcha to the browser, text back

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

## 5. Async

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

## 6. Caching

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

## 7. Error handling

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
| `status` | `sts` | `str \| None` |
| `constitution` | `ctb` | `str \| None` |
| `taxpayer_type` | `dty` | `str \| None` |
| `registration_date` | `rgdt` | `datetime.date \| None` |
| `cancellation_date` | `cxdt` | `datetime.date \| None` |
| `last_updated` | `lstupdt` | `datetime.date \| None` |
| `nature_of_business` | `nba` | `tuple[str, ...]` |
| `principal_address` | `pradr` | `Address \| None` |
| `additional_addresses` | `adadr` | `tuple[Address, ...]` |
| `central_jurisdiction` | `ctj`, `ctjCd` | `Jurisdiction` |
| `state_jurisdiction` | `stj`, `stjCd` | `Jurisdiction` |
| `einvoice_enabled` | `einvoiceStatus` | `bool \| None` |
| `is_field_visit_conducted` | `isFieldVisitConducted` | `bool \| None` |
| `core_business_activity` | `ntcrbs` (code expanded) | `str \| None` |
| `aadhaar_verified` | `adhrVFlag` | `bool \| None` |
| `aadhaar_verified_on` | `adhrVdt` | `datetime.date \| None` |
| `ekyc_status` | `ekycVFlag` | `str \| None` |
| `composition_rate` | `cmpRt` | `str \| None` |
| `raw` | everything | `dict[str, Any]` |

Helpers: `is_active`, `is_cancelled`, `addresses` (principal first),
`as_dict()`, and `unmapped`, which lists portal keys this class does not model, so a new
portal field is never silently dropped.

`Address` carries split fields (`building_name`, `street`, `pincode`, …) *and*
`full`: the portal usually sends the principal address as one `adr` string, so
`as_line()` returns whichever form arrived.

---

# Endpoints and what each costs

| Method | Endpoint | Captcha? |
|---|---|---|
| `fetch_captcha()` | `/services/captcha` | opens the session |
| `fetch_details()` | `/api/search/taxpayerDetails` | **yes**, one per lookup |
| `fetch_goods_and_services()` | `/api/search/goodservice` | no |
| `fetch_financial_years()` | `/api/dropdownfinyear` | no |
| `fetch_filing_preferences()` | `/api/search/taxpayerProfileDetails` | no |
| `fetch_profile()` | all of the above | one |

`goodservice` returns SAC codes for service providers (`bzsdtls`) and HSN
codes for goods (`bzgddtls`); both are parsed into `GoodsOrService`, with
`is_service` telling them apart.

The portal fingerprints clients, so the package sends a browser `User-Agent`
and the `Referer`/`Origin` headers the site expects; without them the captcha
request is reset. For unattended or high-volume use, the official
[GST API](https://developer.gst.gov.in/) through a licensed GSP is the
supported route; this package drives the public, captcha-gated search.

---

# Development

```bash
uv sync              # install, including dev dependencies
uv run pytest        # 46 tests, fully offline via httpx.MockTransport
uv run mypy          # strict
uv run pyright       # strict
uv run ruff check .
```

Tests parse payloads with the exact shape the live portal returns
(`tests/fixtures/`, one service taxpayer and one goods taxpayer, with the
identifying values replaced by fictional ones) and assert `unmapped == {}`,
so a portal schema change fails the suite instead of quietly losing data.

All GSTINs in this README and in the tests are fictional placeholders built
on the dummy PAN `AAACR5055K`; they are checksum-valid but belong to nobody.

## Releasing

CI runs lint, both type checkers, the tests and a build on every push and PR.

To publish a release:

```bash
uv version --bump patch        # or minor / major
git commit -am "Release v$(uv version --short)"
git tag "v$(uv version --short)"
git push origin main --tags
```

The tag triggers `.github/workflows/release.yml`, which re-runs the checks,
builds the sdist and wheel, publishes to PyPI and creates a GitHub release
with generated notes. The workflow refuses to publish if the tag does not
match the version in `pyproject.toml`.

Publishing uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC, no stored secret). One-time setup on PyPI, under
*Your projects -> Publishing* (or *Pending publishers* for a name that does
not exist yet):

| Field | Value |
|---|---|
| PyPI project name | `gst-validator` |
| Owner | `rahulgurujala` |
| Repository name | `gst-validator` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

If a `PYPI_API_TOKEN` repository secret is set instead, the workflow uses that
and skips OIDC.

## License

MIT. See [LICENSE](LICENSE).
