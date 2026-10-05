# Contributing

Thanks for taking the time. Bug reports, portal-shape fixes and docs
improvements are all welcome.

## Getting set up

```bash
git clone https://github.com/rahulgurujala/gst-validator
cd gst-validator
uv sync
uv run pytest
```

[uv](https://docs.astral.sh/uv/) manages the environment; you do not need to
create a virtualenv yourself. Python 3.13+ is required.

## Before opening a pull request

Run what CI runs:

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pyright               # strict type check, second opinion
uv run pytest -q             # tests
```

All five must pass. CI runs the same commands on every push and pull request.

## House rules

- **Tests never hit the network.** Every HTTP call goes through
  `httpx.MockTransport`. If you add an endpoint, add a fixture under
  `tests/fixtures/` with the real payload shape.
- **Never commit a real taxpayer's data.** Fixtures use either a public
  company's registration or a fictional, checksum-valid GSTIN. Do not add
  someone's name, address or GSTIN to this repo.
- **Keep it typed.** Both type checkers run in strict mode. Public functions
  are annotated; `Any` only at the JSON boundary, narrowed immediately.
- **Model the portal, do not guess it.** New fields come from an observed
  response. Keep the raw body in `TaxpayerDetails.raw`, add the key to
  `_MAPPED_KEYS`, and let the `unmapped == {}` test prove nothing was dropped.

## When the portal changes

The GST portal is undocumented and can change without notice. If a field
stops parsing:

1. Capture the real body with `gst-validator <GSTIN> --raw`.
2. Strip anything identifying and add it to `tests/fixtures/`.
3. Add the key to the model and to `_MAPPED_KEYS`.

A failing `unmapped == {}` assertion is the intended signal, not a flake.

## Commit messages

Describe the change and why. No AI attribution lines.

## Releasing (maintainers)

```bash
uv version --bump patch      # or minor / major
git commit -am "Release v$(uv version --short)"
git tag "v$(uv version --short)"
git push origin main --tags
```

The tag triggers the release workflow: checks, build, PyPI publish via Trusted
Publishing, then a GitHub release. The workflow refuses to publish if the tag
does not match the version in `pyproject.toml`.
