<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/rahulgurujala/gst-validator/main/assets/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/rahulgurujala/gst-validator/main/assets/logo.svg" alt="gst-validator" width="128" height="128">
</picture>

# gst-validator

**Validate Indian GSTINs offline and pull taxpayer details from the public GST portal.**

[![PyPI](https://img.shields.io/pypi/v/gst-validator?color=0d7377&label=pypi)](https://pypi.org/project/gst-validator/)
[![Python](https://img.shields.io/pypi/pyversions/gst-validator)](https://pypi.org/project/gst-validator/)
[![CI](https://github.com/rahulgurujala/gst-validator/actions/workflows/ci.yml/badge.svg)](https://github.com/rahulgurujala/gst-validator/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/gst-validator?color=blue)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict-blue)](https://peps.python.org/pep-0561/)
[![Downloads](https://img.shields.io/pypi/dm/gst-validator?color=777)](https://pypi.org/project/gst-validator/)
[![Visitors](https://hits.sh/github.com/rahulgurujala/gst-validator.svg?style=flat&label=visitors&color=777)](https://hits.sh/github.com/rahulgurujala/gst-validator/)

[Install](#install) · [CLI](#cli) · [Library](#using-it-in-your-app) · [Data](#what-you-get-back) · [Contributing](#contributing)

</div>

---

> [!IMPORTANT]
> **Unofficial, and published for educational and experimental use.**
>
> This project is not affiliated with, endorsed by, or supported by the Goods
> and Services Tax Network (GSTN), the Government of India, or any tax
> authority. It talks to undocumented endpoints of the public GST portal,
> which may change, rate-limit, or stop responding at any time, and it makes
> no claim that its results are complete, current, or correct.
>
> Do not treat its output as an authoritative record. Verify anything that
> matters against the official portal before relying on it for compliance,
> invoicing, onboarding, or any other legal or financial decision.
>
> You are responsible for how you use it, including compliance with the
> portal's terms of use, applicable law, and data-protection obligations for
> any taxpayer data you retrieve. The software is provided "as is", without
> warranty of any kind, and the authors accept no liability for any claim,
> damage, or loss arising from its use. See [LICENSE](LICENSE).
>
> For unattended or high-volume access, use the official
> [GST API](https://developer.gst.gov.in/) through a licensed GSP.

A typed Python library and CLI for the Indian GST taxpayer search. It checks a
GSTIN's structure and checksum without touching the network, and wraps the
portal's undocumented endpoints in objects you can actually hold.

```python
from gst_validator import GSTIN, GSTClient

GSTIN.is_valid("27AAACR5055K1Z7")  # True, offline, no network

with GSTClient() as client:
    client.fetch_goods_and_services("27AAACR5055K1Z7")  # no captcha needed
```

## Why this exists

The GST portal has no public API for taxpayer search. What it has is a
captcha-gated web form and a handful of undocumented JSON endpoints that
return `"NA"` for null, `dd/mm/yyyy` for dates, two different shapes for the
same field, and HTTP 200 for errors. This package absorbs that so your code
sees `datetime.date`, `None` and exceptions.

| | |
|---|---|
| **Offline validation** | Format and mod-36 checksum, plus state, PAN or TAN, entity and registration type decoded from the number |
| **Typed objects** | Dates parsed, `"NA"` / `""` / `null` normalised, nothing silently dropped |
| **Captcha, your way** | Raw bytes, base64 or a `data:` URI, so a browser, a human or a service can solve it |
| **Three free endpoints** | HSN/SAC codes, financial years and filing preferences need no captcha at all |
| **Sync and async** | The same API with `await`, both strict-typed and `py.typed` |
| **Caching built in** | A lookup costs a human-solved captcha, so results are cached by default |

## What it does

| | |
|---|---|
| **Validates offline** | Format *and* mod-36 checksum, plus state, PAN or TAN, entity and registration type decoded from the number. No network, no rate limit |
| **Knows every layout** | Ordinary, TDS deductor, TCS collector, UIN (UN bodies and embassies) and the separate one used by non-resident online-service providers |
| **Bulk by default** | A CSV column, a file of GSTINs or stdin; CSV, JSON, JSON Lines or table out |
| **Typed objects** | Dates parsed, `"NA"` normalised, nothing silently dropped, `py.typed` shipped |
| **Captcha, your way** | Raw bytes, base64 or a `data:` URI, so a browser, a person or a service can solve it |
| **Three free endpoints** | HSN/SAC codes, financial years and filing preferences need no captcha at all |
| **Sync and async** | The same API with `await`, both strict-typed |
| **Caches properly** | A lookup costs a human-solved captcha, so results persist between CLI runs |

## Documentation

| Guide | What is in it |
|---|---|
| **[CLI guide](docs/cli.md)** | Every flag, the five output formats, batching, exit codes |
| **[Library guide](docs/library.md)** | The Python API, layouts, caching, errors, and every field returned |
| **[Recipes](docs/recipes.md)** | Whole tasks: checking a supplier spreadsheet, serving a web app, enriching a list |
| **[Portal reference](docs/portal.md)** | What the portal actually returns, its quirks, and how each claim was verified |

## A taste of it

Validate a whole spreadsheet column without touching the network:

```bash
gst-validator - --offline --column gstin --format csv < suppliers.csv > checked.csv
```

Add the portal data that needs no captcha:

```bash
gst-validator - --offline --enrich --format json < gstins.txt -o enriched.json
```

Look one up properly, solving a captcha once:

```bash
gst-validator 27AAACR5055K1Z7 --json | jq -r .legal_name
```

From Python:

```python
from gst_validator import GSTIN, validate_many

GSTIN.is_valid("27AAACR5055K1Z7")  # True, offline

for row in validate_many(["27AAACR5055K1Z7", "nope"]):
    print(row.value, row.is_valid, row.error)
```

## Install

```bash
uv add gst-validator          # into a uv project
pip install gst-validator     # or plain pip
uvx gst-validator --help      # or run it without installing
```

Python 3.13 or newer. Two runtime dependencies:
[httpx](https://www.python-httpx.org/) for the HTTP layer and
[rich](https://rich.readthedocs.io/) for the CLI output.

Then read the [CLI guide](docs/cli.md) or the
[library guide](docs/library.md), depending on how you mean to use it.

## Development

```bash
uv sync              # install, including dev dependencies
uv run pytest        # 175 tests, fully offline via httpx.MockTransport
uv run mypy          # strict
uv run pyright       # strict
uv run ruff check .
```

Tests parse payloads with the exact shape the live portal returns
(`tests/fixtures/`: a service provider, a manufacturer, a statutory body, a
cancelled registration and a goods list, with the
identifying values replaced by fictional ones) and assert `unmapped == {}`,
so a portal schema change fails the suite instead of quietly losing data.

All GSTINs in this README and in the tests are fictional placeholders built
on the dummy PAN `AAACR5055K`; they are checksum-valid but belong to nobody.

## Releasing

Releases are automated with
[release-please](https://github.com/googleapis/release-please). Commits on
`main` follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `portal:`, ...); a bot keeps a release pull request up to
date with the next version number and the changelog. Merging it bumps the
version, tags, publishes to PyPI and creates the GitHub release. Nothing is
tagged or edited by hand. Details in [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Issues and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the house rules; the
short version:

```bash
uv sync
uv run pytest -q && uv run ruff check . && uv run mypy && uv run pyright
```

Two rules matter more than the rest: **tests never touch the network**
(everything goes through `httpx.MockTransport`), and **no real taxpayer's data
in the repo** - fixtures use a public company's registration or a fictional,
checksum-valid GSTIN.

If the portal changes shape, that is a
[portal change issue](https://github.com/rahulgurujala/gst-validator/issues/new?template=portal_change.yml);
include the output of `--raw` with the identifying values replaced.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

Security reports go through
[private advisories](https://github.com/rahulgurujala/gst-validator/security/advisories/new),
not public issues. See [SECURITY.md](SECURITY.md).

## Links

- [CLI guide](docs/cli.md) · [Library guide](docs/library.md) · [Recipes](docs/recipes.md) · [Portal reference](docs/portal.md)
- [PyPI](https://pypi.org/project/gst-validator/)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)
- [Official GST developer portal](https://developer.gst.gov.in/) - the licensed GSP route for unattended, high-volume use

## License

MIT. See [LICENSE](LICENSE).
