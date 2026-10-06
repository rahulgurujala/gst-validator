# Contributing

Thanks for taking the time. Bug reports, portal-shape fixes and docs
improvements are all welcome. Taking part means following the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Getting set up

```bash
git clone https://github.com/rahulgurujala/gst-validator
cd gst-validator
uv sync
uv run pytest
```

[uv](https://docs.astral.sh/uv/) manages the environment; you do not need to
create a virtualenv yourself. Python 3.13+ is required.

## Where things live

```
src/gst_validator/
  gstin.py       the number itself: the three layouts, checksum, what each encodes
  taxpayer.py    what the portal returns: details, address, jurisdiction, profile
  captcha.py     the captcha image
  bulk.py        validating and enriching many at once
  client.py      the sync and async HTTP clients
  cache.py       the in-memory and on-disk caches
  cli.py         the command line, a thin shell over the rest
  exceptions.py  the one exception tree
  _parsing.py    shared normalisers for the portal's loose JSON
  models.py      re-exports the types, so older imports keep working

scripts/
  check_state_master.py  diffs the state table against the portal's own
                         master; a maintainer tool, run before a release
```

Tests mirror that: `test_gstin.py`, `test_taxpayer.py`, `test_client.py`,
`test_cache.py`, `test_bulk.py`, `test_cli.py` and `test_packaging.py`, with
shared payloads and helpers in `tests/support.py`.

## Before opening a pull request

Run what CI runs:

```bash
uv lock --check              # the lockfile matches pyproject.toml
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pyright               # strict type check, second opinion
uv run pytest -q             # tests
```

All of these must pass. CI runs the same commands on every push and pull
request, and installs with `uv sync --locked` so a stale lockfile fails there
rather than silently resolving to something else.

The state table in `gstin.py` is hardcoded so that decoding stays offline.
It goes stale when the portal changes - Ladakh was added in 2019, and Dadra
and Nagar Haveli merged with Daman and Diu in 2020 - so run
`uv run scripts/check_state_master.py` before a release. It is not a test,
because tests never touch the network.

If you change dependencies, run `uv lock` and commit `uv.lock` with the
change. You do not need to touch it for a release: the release workflow
refreshes it on the release pull request, because release-please bumps this
project's version in `pyproject.toml` but does not know that `uv.lock` records
that version too.

## House rules

- **Tests never hit the network.** Every HTTP call goes through
  `httpx.MockTransport`. If you add an endpoint, add a fixture under
  `tests/fixtures/` with the real payload shape. The modules are listed under
  [Where things live](#where-things-live).
- **Never commit a real taxpayer's data.** Fixtures use either a public
  company's registration or a fictional, checksum-valid GSTIN. Do not add
  someone's name, address or GSTIN to this repo.
- **Keep it typed.** Both type checkers run in strict mode. Public functions
  are annotated; `Any` only at the JSON boundary, narrowed immediately.
- **Machine output stays plain.** The human table is rendered with rich, but
  `--json` and `--raw` are written with `print()` so pipes and `jq` get
  byte-exact output. Progress messages belong on stderr.
- **Model the portal, do not guess it.** New fields come from an observed
  response, never from a blog post. Add the key to the model and to that
  model's key set (`_MAPPED_KEYS`, `_REGISTRATION_KEYS`), and let the
  `unmapped == {}` tests prove nothing was dropped.
- **Keep a raw body only where something round-trips it.**
  `TaxpayerDetails.raw` exists because the disk cache stores it and must
  survive the models growing. `Registration` keeps none: every key it
  receives has a field, and `unmapped` carries anything new. A second copy of
  a payload nothing re-reads is weight without a job.

## When the portal changes

The GST portal is undocumented and can change without notice. If a field
stops parsing:

1. Capture the real body with `gst-validator <GSTIN> --raw`.
2. Strip anything identifying and add it to `tests/fixtures/`.
3. Add the key to the model and to that model's key set.

A failing `unmapped == {}` assertion is the intended signal, not a flake.

## Commit messages

This repository releases itself from commit messages, so they follow
[Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add fetch_returns() for the filing history endpoint
fix: parse pradr when the portal sends split address fields
portal: handle bzgddtls for goods taxpayers
docs: explain the captcha session lifetime
chore: bump ruff
```

| Prefix | Effect on the next release | Appears in the changelog as |
|---|---|---|
| `feat:` | minor bump (0.1.0 -> 0.2.0) | Added |
| `fix:` | patch bump (0.1.0 -> 0.1.1) | Fixed |
| `portal:` | patch bump | Portal changes |
| `perf:` | patch bump | Performance |
| `deps:` | patch bump | Dependencies |
| `docs:`, `ci:`, `test:`, `refactor:`, `chore:` | no release | hidden |
| `feat!:` or a `BREAKING CHANGE:` footer | major bump once past 1.0 | Breaking |

Anything else is ignored by the release tooling, so a commit with no prefix
never ships. Do not edit `CHANGELOG.md` or the version in `pyproject.toml` by
hand; both are written for you. And no AI attribution lines.

## Releasing (maintainers)

There is nothing to run. On every push to `main`,
[release-please](https://github.com/googleapis/release-please) opens or
updates a pull request titled "chore(main): release X.Y.Z" that contains the
version bump and the changelog entries for everything merged since the last
release.

Merging that pull request:

1. bumps the version in `pyproject.toml` and `src/gst_validator/__init__.py`,
2. writes `CHANGELOG.md`,
3. tags `vX.Y.Z` and creates the GitHub release,
4. builds, publishes to PyPI via Trusted Publishing, and attaches the sdist
   and wheel to the release.

To hold a release back, leave the pull request unmerged; it keeps collecting
changes.
