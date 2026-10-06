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
| `--enrich` | Add the captcha-free portal data to each row; needs the network, even alongside `--offline` |
| `--details-only` | Skip the captcha-free extras on a full lookup |
| `--refresh` | Ignore any cached result and look up afresh |
| `--no-cache` | Neither read nor write the on-disk cache |
| `--clear-cache` | Delete every cached lookup and exit |
| `--captcha-path PATH` | Where to write the captcha image |
| `--captcha-base64` | Print the captcha as a `data:` URI instead of a file |
| `--keep-captcha` | Keep the captcha image after it has been solved |
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
