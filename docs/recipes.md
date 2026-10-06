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

## Find every registration a supplier holds

A company registers once per state against one PAN, so the GSTIN on an
invoice is rarely the whole relationship. The PAN is inside that GSTIN, so one
number is enough to find the rest:

```bash
pan=$(gst-validator 27AAACR5055K1Z7 --offline --json | jq -r .pan)
gst-validator --pan "$pan" --format csv -o registrations.csv
```

The first command touches nothing; the second costs one captcha. In Python,
the same two steps:

```python
from gst_validator import GSTClient, GSTIN

pan = GSTIN.parse("27AAACR5055K1Z7").pan

with GSTClient() as client:
    captcha = client.fetch_captcha()
    solved = input(f"solve this: {captcha.data_uri}\n> ")
    registrations = client.fetch_registrations_by_pan(pan, solved)

for reg in registrations:
    state = reg.number.state_name if reg.number else reg.state_code
    print(f"{reg.gstin}  {state}  {'active' if reg.is_active else reg.status}")
```

Useful on the result:

```python
# which states is this supplier actually trading in today?
active = [r for r in registrations if r.is_active]

# a cancelled registration you are still invoicing against is worth knowing
dormant = [r for r in registrations if not r.is_active]

# several registrations in one state are normal; the 13th character orders them
by_state: dict[str, list[str]] = {}
for reg in registrations:
    by_state.setdefault(reg.state_code or "??", []).append(reg.gstin)
```

Each PAN costs its own captcha, and the portal makes each captcha single-use,
so this is a per-supplier operation rather than something to run over a whole
spreadsheet.

## Check whether a supplier is on the composition scheme

A composition dealer pays a flat rate and **cannot charge you GST you can
reclaim**. If one invoices you with tax on it, that is a problem worth
catching early.

```bash
gst-validator --composition --state 27 --year 2025-2026 --format csv -o comp.csv
```

One captcha covers a whole state and year, so this is cheap per supplier. In
Python, to screen a list of GSTINs against it:

```python
from gst_validator import GSTClient

with GSTClient() as client:
    solved = input(f"solve: {client.fetch_captcha().data_uri}\n> ")
    scheme = {row.gstin for row in client.search_composition_taxpayers("27", "2025-2026", solved)}

flagged = [gstin for gstin in my_suppliers if gstin in scheme]
```

A taxpayer's own lookup says the same thing in `taxpayer_type`, but that costs
one captcha each; this costs one for the whole state.

Each row gives the GSTIN, the legal name and the dates the scheme applied
from and to. An empty result means nobody matched that state and year, not
that something failed. Many of these are sole proprietors, so the names are
often individuals: treat the output as personal data.

## Look a commodity code up

Free, so there is nothing to ration:

```bash
gst-validator --hsn 3926
gst-validator --hsn plastic --by description
```

```python
from gst_validator import GSTClient

with GSTClient() as client:
    for code in client.search_hsn_codes("9983"):
        print(code.code, "service" if code.is_service else "goods", code.description)
```

This pairs with `fetch_goods_and_services()`, which returns the codes a
taxpayer is registered for but sometimes with a thin description.

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

## Check whether a notice is genuine

Fake GST notices are a known problem. Every real one carries a reference
number (RFN), and the portal will say whether it issued it:

```bash
gst-validator --rfn RF2701250000001 --json
```

```python
from gst_validator import GSTClient

with GSTClient() as client:
    solved = input(f"solve: {client.fetch_captcha().data_uri}\n> ")
    notice = client.verify_reference_number("RF2701250000001", solved)

if notice.is_genuine:
    print("issued by the department on", notice.issued_on)
else:
    print("the department has no record of this reference")
```

A reference the portal does not recognise comes back with `is_genuine` false
rather than raising, because "we have never seen this" is the answer you came
for. Only a portal-level failure, such as a wrong captcha, raises.

## Track an application you have filed

```bash
gst-validator --arn AA270125000000X
```

Returns the form, the status and the dates. One captcha.

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
