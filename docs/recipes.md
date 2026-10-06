# Recipes

Whole tasks, start to finish. Each one is runnable as written.

## Check a supplier spreadsheet

The common case: a CSV with a GSTIN column, and you want to know which are
malformed before anything reaches your accounts system. No captcha, no portal,
no rate limit - this is pure arithmetic.

```bash
gst-validator - --offline --column gstin --format csv < suppliers.csv > checked.csv
```

`checked.csv` keeps every original column and adds `valid`, `error`, the
decoded state, PAN and entity type. The exit code is `2` if any row failed, so
it drops into a pipeline:

```bash
if ! gst-validator - --offline --column gstin --format csv < suppliers.csv > checked.csv; then
    echo "some GSTINs are malformed, see checked.csv"
fi
```

In Python, when you want the rows rather than a file:

```python
import csv

from gst_validator import validate_many

with open("suppliers.csv", newline="") as handle:
    records = list(csv.DictReader(handle))

rows = validate_many(
    (record["gstin"] for record in records),
    extras=({k: v for k, v in record.items() if k != "gstin"} for record in records),
)
bad = [row for row in rows if not row.is_valid]
print(f"{len(bad)} of {len(records)} need attention")
```

## Screen a GSTIN before you spend a captcha

Validation is free and catches typos, wrong state codes and transposed
characters. Do it first, always:

```python
from gst_validator import GSTIN, InvalidGSTINError


def screen(value: str) -> str | None:
    try:
        return GSTIN.parse(value).value  # normalised: stripped, upper-cased
    except InvalidGSTINError as error:
        print(f"skipping {value}: {error.reason}")
        return None
```

## Enrich a list without solving anything

Three endpoints answer without a captcha: the HSN/SAC codes a taxpayer is
registered for, the financial years they have filed, and whether they file
monthly or quarterly.

```bash
gst-validator - --offline --enrich --format csv < gstins.txt -o enriched.csv
```

```python
from gst_validator import enrich_many, validate_many

for row in enrich_many(validate_many(gstins)):
    if row.enrichment_error:
        print(row.gstin, "could not be enriched:", row.enrichment_error)
        continue
    print(row.gstin, [str(code) for code in row.goods_and_services])
```

One session serves the whole batch, and a row that fails records the reason
instead of ending the run.

## Look one up properly, with the captcha

```bash
gst-validator 27AAACR5055K1Z7
```

It writes the captcha image to a temporary file, prints the path, waits for
you to type what it says, then prints the taxpayer. The image is deleted once
you have entered it.

To solve it somewhere else - a browser, another screen, a person in a
different room - ask for the image as a `data:` URI instead:

```bash
gst-validator 27AAACR5055K1Z7 --captcha-base64 --json -o taxpayer.json
```

The URI and the prompt go to stderr, so `taxpayer.json` still holds nothing
but JSON.

## Serve it from a web app

The captcha is bound to the session that fetched it, so keep one client per
pending lookup:

```python
import uuid

from fastapi import FastAPI, HTTPException

from gst_validator import GSTClient, GSTValidatorError, InvalidGSTINError

app = FastAPI()
pending: dict[str, GSTClient] = {}  # use Redis and a TTL in production


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

`captcha.data_uri` goes straight into `<img src="...">`. Give `pending` an
expiry: an abandoned entry holds a connection pool open.

## Keep results between runs

Each lookup costs a human-solved captcha, so the CLI caches to disk for 24
hours. A second run on the same GSTIN costs nothing:

```bash
gst-validator 27AAACR5055K1Z7 --json      # solves a captcha
gst-validator 27AAACR5055K1Z7 --json      # served from the cache
gst-validator 27AAACR5055K1Z7 --refresh   # ignore it and look up again
gst-validator --clear-cache               # forget everything
```

Those files hold a registered name and place of business. See
[the library guide](library.md) for where they live and how to swap in Redis.

## Pull the portal's own body

When a field looks wrong, compare against what actually arrived:

```bash
gst-validator 27AAACR5055K1Z7 --raw | jq 'keys'
```

In Python, `TaxpayerDetails.raw` is always the untouched payload, and
`unmapped` is every key this package does not model yet - if that is not
empty, the portal has grown a field:

```python
if details.unmapped:
    print("portal sent something new:", details.unmapped)
```

## Tell the layouts apart

Not every GSTIN is PAN-based. An overseas supplier invoicing you from Upwork
or GoDaddy has a different shape entirely, and so does an embassy:

```python
from gst_validator import GSTIN, GSTINLayout

match GSTIN.parse(value).layout:
    case GSTINLayout.PAN:
        ...  # pan, entity_type, registration_type apply
    case GSTINLayout.NON_RESIDENT:
        ...  # holder_code is a country, e.g. "USA"
    case GSTINLayout.UIN:
        ...  # a UN body or diplomatic mission
```

See [the portal reference](portal.md) for what each layout encodes.
