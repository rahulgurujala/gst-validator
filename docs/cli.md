# CLI guide

`gst-validator` validates GSTINs offline and, when you ask it to, looks them
up on the GST portal. It is a thin shell over the library: anything here is
available from Python too, as [the library guide](library.md) shows.

Run `gst-validator --help` for the full flag list with worked examples.

```
usage: gst-validator [-h] [--version] [--offline]
                     [-f {table,json,jsonl,csv,raw}] [--json] [--column NAME]
                     [-o PATH] [--pan PAN] [--enrich] [--details-only] [--raw]
                     [--captcha-path CAPTCHA_PATH] [--captcha-base64]
                     [--no-cache] [--clear-cache] [--refresh] [--keep-captcha]
                     [--no-color]
                     [gstin ...]
```

## Validate without touching the network

```bash
$ gst-validator 27AAACR5055K1Z7 --offline
valid 27AAACR5055K1Z7
state code             27
state name             Maharashtra
identifier             AAACR5055K
identifier type        PAN
pan                    AAACR5055K
entity type            Company
registration sequence  1
registration type      Regular

$ gst-validator 27AAACR5055K1Z7 --offline --json
{
  "input": "27AAACR5055K1Z7",
  "valid": true,
  "error": null,
  "gstin": "27AAACR5055K1Z7",
  "state_code": "27",
  "state_name": "Maharashtra",
  "is_union_territory": false,
  "identifier": "AAACR5055K",
  "identifier_type": "PAN",
  "pan": "AAACR5055K",
  "tan": null,
  "entity_type": "Company",
  "registration_sequence": "1",
  "registration_type": "Regular",
  "layout": "pan"
}
```

Use this in CI, in a pre-commit check, or to screen input before spending a
captcha. Exit code `2` means the GSTIN is malformed.

## Full lookup (interactive)

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

## Machine-readable output

```bash
gst-validator 27AAACR5055K1Z7 --json     # modelled fields, dates as ISO strings
gst-validator 27AAACR5055K1Z7 --raw      # the portal's body verbatim, nothing dropped
```

`--json` is the one to parse: stable key names, `null` instead of `"NA"`,
dates as `2025-09-15`. `--raw` is for debugging what the portal actually sent.

The human-facing table is rendered with [rich](https://rich.readthedocs.io/),
but `--json` and `--raw` are written as plain text with no styling or
wrapping, so they stay byte-exact. Results go to stdout and progress messages
to stderr, which makes piping safe:

```bash
gst-validator 27AAACR5055K1Z7 --json | jq -r '.legal_name, .principal_address'
```

## Solving the captcha somewhere else

```bash
$ gst-validator 27AAACR5055K1Z7 --captcha-base64
data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALY...
captcha text: 784077
```

The data URI and the prompt go to **stderr**, never stdout, so `--json` and
`--raw` stay pipeable while you solve the captcha. Paste the URI into a
browser address bar, drop it in an `<img src=...>`, or hand it to a solving
service. The process keeps the portal session open while it waits on stdin,
which is what makes this work.

```bash
gst-validator 27AAACR5055K1Z7 --json --captcha-base64 > taxpayer.json
# the URI and the prompt appear on your terminal; only JSON reaches the file
```

## Every registration a company holds

A company registers once per state, all on the same PAN. `--pan` lists them
all, which is the quickest way to find a supplier's other GSTINs:

```bash
$ gst-validator --pan AAACR5055K
captcha image written to /tmp/AAACR5055K-captcha.png   # stderr
captcha text: 784077
gstin            status    state         type
24AAACR5055K2ZC  Inactive  Gujarat       Regular
14AAACR5055K1ZE  Active    Manipur       Regular
32AAACR5055K1ZG  Active    Kerala        Regular
...
```

The PAN is checked for shape *before* a captcha is fetched, so a typo costs
you nothing. One captcha covers one PAN; the portal makes each one single-use,
so a second PAN needs a second captcha.

Every output format works, and each row carries the state and registration
type decoded from the number itself:

```bash
gst-validator --pan AAACR5055K --json        # always a list, even for one row
gst-validator --pan AAACR5055K --format csv -o registrations.csv
```

The PAN of a GSTIN you already hold is on the object, so the two chain:

```bash
pan=$(gst-validator 27AAACR5055K1Z7 --offline --json | jq -r .pan)
gst-validator --pan "$pan" --json
```

## The portal's other searches

### Commodity and service codes

No captcha, no session, no limit - so a batch costs nothing:

```bash
gst-validator --hsn 3926                                 # by code
gst-validator --hsn plastic --by description             # goods, by words
gst-validator --hsn transport --by description --services  # service codes
gst-validator --hsn - --format csv < codes.txt > described.csv
```

A code search covers goods and services together. A description search does
not: the portal wants one or the other, so `--services` switches from HSN to
SAC.

`is_service` tells SAC codes from HSN codes, derived from the leading `99`
rather than from any flag the portal sends.

### Composition-scheme taxpayers

A composition dealer cannot charge you GST you are able to reclaim, so this is
worth knowing about a supplier. One captcha per state and year:

```bash
gst-validator --composition --state 27 --year 2025-2026
gst-validator --composition --state 27 --year 2025-2026 --opted-out
```

### Track an application, verify a notice

```bash
gst-validator --arn AA270125000000X      # where an application has reached
gst-validator --rfn RF2701250000001      # was this notice really issued?
gst-validator --temp-id 271700000000TMP  # a temporary registration
```

Each costs one captcha. `--rfn` is the one to know about: fake GST notices are
a real problem, and a reference the portal does not recognise comes back with
`is_genuine: false` rather than an error, because that is the answer.

### Find a GST practitioner

```bash
gst-validator --practitioner --enrolment 351800000001GP9
gst-validator --practitioner --state 35 --pincode 744103
```

No captcha. The results describe named individuals, so the search has to be
narrowed: either `--enrolment` for one person, or `--state` for an area.
A pincode on its own is refused by the portal, so it narrows `--state`
rather than replacing it. The portal also
publishes a phone number and an email address for each practitioner; this
package does not carry either, because engaging one is done through the
portal.

## Validating many at once

Offline validation needs no captcha, so a whole spreadsheet column costs
nothing:

```bash
# one per line
gst-validator 27AAACR5055K1Z7 29AAACI4798L1ZU --offline

# a CSV column, with the other columns carried through to the output
gst-validator - --offline --column gstin --format csv < suppliers.csv > checked.csv

# add the portal data that needs no captcha
gst-validator - --offline --enrich --format json < gstins.txt -o enriched.json
```

`--column NAME` reads the input as CSV and takes GSTINs from that column; every
other field in the row is carried into the output, so the result lines up with
what you started from. Blank cells are skipped, and a missing column is
reported rather than guessed at.

Two details that matter with real spreadsheets:

- A file exported from Excel starts with a byte-order mark. It is stripped, so
  `--column gstin` matches whether or not the mark is there.
- If one of your columns is named the same as one of the computed ones
  (`valid`, `state_name`, and so on), yours is carried through as
  `source_valid`, `source_state_name` and so on, rather than being
  overwritten.
- `--column` checks format and checksum; it does **not** look each row up on
  the portal, because a file of five hundred rows would mean five hundred
  captchas. It says so when you run it. Add `--enrich` for the portal data
  that needs no captcha - that one does call the portal, just without ever
  asking you to solve anything.

A row that fails never stops the run: it comes back with `valid: false` and an
`error`, and the exit code is `2` if any row was bad.

## Output formats

| `--format` | What it is | Good for |
|---|---|---|
| `table` | rich, human-readable (the default) | reading on a terminal |
| `json` | one object, or an array for several inputs | `jq`, APIs |
| `jsonl` | one object per line | streaming, pandas, large files |
| `csv` | header plus one row per input | spreadsheets |
| `raw` | the portal's own body, unchanged | debugging the portal |

`--json` and `--raw` remain as shorthands. Only `table` is styled; every other
format is written as plain text with no colour or wrapping, so piping and
redirection stay byte-exact. Results go to stdout and progress to stderr, and
`-o PATH` writes the result to a file instead:

```bash
gst-validator 27AAACR5055K1Z7 --offline --format csv -o checked.csv
```

One GSTIN prints the detailed vertical view; several print a line each, which
is what you want when scanning a column.

## Looking up several in one session

A full lookup needs a captcha each, but the session is shared, so a batch
opens one connection and prompts you per GSTIN with a counter:

```bash
gst-validator 27AAACR5055K1Z7 29AAACI4798L1ZU --format json -o both.json
```

A lookup that fails is reported and the run carries on with the next one.

## Other flags

```bash
gst-validator 27AAACR5055K1Z7 --details-only   # skip the captcha-free extras
gst-validator 27AAACR5055K1Z7 --refresh        # ignore the cache, force a fresh lookup
gst-validator 27AAACR5055K1Z7 --keep-captcha   # keep the image file for inspection
gst-validator 27AAACR5055K1Z7 --captcha-path ./c.png   # write it where you want
gst-validator 27AAACR5055K1Z7 --no-color               # plain text, no styling
gst-validator 27AAACR5055K1Z7 --no-cache               # skip the on-disk cache
gst-validator --clear-cache                            # forget every cached lookup
gst-validator --version                                # print the version
```

Also runnable as a module: `python -m gst_validator 27AAACR5055K1Z7`.

## Every flag

| Flag | What it does |
|---|---|
| `--offline` | Validate format and checksum only, with no captcha lookup |
| `-f`, `--format` | `table`, `json`, `jsonl`, `csv` or `raw` |
| `--json` / `--raw` | Shorthands for `--format json` / `--format raw` |
| `-o`, `--output PATH` | Write the result to a file instead of stdout |
| `--column NAME` | Read the input as CSV and take GSTINs from this column |
| `--pan PAN` | List every GSTIN registered under this PAN; costs one captcha |
| `--hsn TEXT` | Search HSN/SAC codes, with `--by code` or `--by description`; no captcha |
| `--practitioner` | Find GST practitioners; needs `--enrolment`, or `--state` with an optional `--pincode` |
| `--composition` | Composition-scheme list; needs `--state` and `--year`, one captcha |
| `--opted-out` | With `--composition`, list those who left the scheme instead |
| `--state CODE`, `--pincode PIN`, `--year FY`, `--enrolment NO` | Narrow the searches above |
| `--by {code,description}` | How `--hsn` matches (default: `code`) |
| `--services` | With `--hsn --by description`, search SAC codes instead of HSN |
| `--arn ARN` | Track an application; one captcha |
| `--rfn REF` | Verify a document reference number; one captcha |
| `--temp-id ID` | Look up a temporary registration; one captcha |
| `--enrich` | Add the captcha-free portal data to each row; needs the network, even alongside `--offline` |
| `--details-only` | Skip the captcha-free extras on a full lookup |
| `--refresh` | Ignore any cached result and look up afresh |
| `--no-cache` | Neither read nor write the on-disk cache |
| `--clear-cache` | Delete every cached lookup and exit |
| `--captcha-path PATH` | Where to write the captcha image |
| `--captcha-base64` | Print the captcha as a `data:` URI instead of a file |
| `--keep-captcha` | Keep the captcha image after it has been solved |
| `--proxy URL` | Send requests through a proxy, e.g. an egress or debugging one |
| `--min-interval SECONDS` | Smallest gap between requests (default 1s) |
| `--no-color` | Plain text, no styling (also honours `NO_COLOR`) |
| `--version` | Print the version |

## How output is split

Results go to **stdout**. Progress, prompts, captcha paths and errors go to
**stderr**. That is what makes this safe:

```bash
gst-validator 27AAACR5055K1Z7 --json > taxpayer.json   # only JSON in the file
gst-validator - --offline --format csv < in.csv | column -t -s,
```

Only `table` is styled. `json`, `jsonl`, `csv` and `raw` are written as plain
text with no colour and no wrapping, so they stay byte-exact through a pipe.

## One mode at a time

Each search is its own mode, and a flag belonging to another one is refused
rather than quietly ignored:

```bash
$ gst-validator --pan AAACR5055K --offline
--offline does not apply to --pan          # exit 2, no captcha spent

$ gst-validator --pan AAACR5055K --hsn 3926
pick one of --pan, --hsn                   # exit 2
```

`--format`, `-o`, `--no-color` and the captcha flags apply everywhere. The
rest belong to one mode: `--offline`, `--column`, `--enrich`, `--details-only`
and `--refresh` to a plain GSTIN lookup, `--by` and `--services` to `--hsn`,
`--enrolment` and `--pincode` to `--practitioner`, `--year` and `--opted-out`
to `--composition`, and `--state` to either of the last two.

## Pacing, and the firewall

The portal sits behind a firewall that blocks a whole address after a burst of
requests, and it does not spare the captcha-free endpoints. So every client
leaves **one second between requests** by default:

```bash
gst-validator --hsn - --min-interval 2 < codes.txt   # slower still
gst-validator --hsn 3926 --min-interval 0            # only against a fake portal
```

This costs nothing on the captcha-gated searches, where a person is solving an
image between calls anyway. It does pace a long `--hsn` batch, which is the
point.

If you are blocked, you will see:

```
the portal's firewall rejected this client, which it does after a burst of
requests from one address; it clears on its own, so wait and retry
```

**Wait it out.** The block is on the address, not the session, so a new client
will not help. Changing address to get around it is not what `--proxy` is for.

### `--proxy`

```bash
gst-validator --hsn 3926 --proxy http://proxy.internal:3128
```

For the ordinary reasons: a network that requires an egress proxy, or pointing
at something like mitmproxy while debugging. It is passed straight to httpx.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything succeeded |
| `1` | A lookup failed: wrong captcha, portal error, network |
| `2` | At least one input was not a valid GSTIN |
| `130` | Aborted with Ctrl-C, or stdin closed at a prompt |
| `141` | The reader closed the pipe, as `head` does. Not an error |

In a batch the worst code wins, so one bad row in a thousand still gives `2`
while every good row is reported normally.

```bash
if gst-validator "$GSTIN" --offline >/dev/null 2>&1; then
  echo "well-formed"
fi
```
