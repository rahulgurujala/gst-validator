# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-05

First release.

### Added

- `GSTIN` value object: format and mod-36 checksum validation, with state,
  PAN, entity type and registration sequence decoded from the number.
- `GSTClient` and `AsyncGSTClient` over httpx, covering the captcha-gated
  `taxpayerDetails` lookup plus three endpoints that need no captcha:
  `goodservice`, `dropdownfinyear` and `taxpayerProfileDetails`.
- Structured results: `TaxpayerDetails`, `TaxpayerProfile`, `Address`,
  `Jurisdiction`, `GoodsOrService`, `FinancialYear`, `FilingPreference`, with
  dates parsed and `"NA"` / `""` normalised to `None`.
- `Captcha` with `content`, `base64` and `data_uri`, so the image can be
  solved in a browser or by a service.
- Process-wide `TTLCache` behind a `TaxpayerCache` protocol, plus `NullCache`.
- `gst-validator` CLI: offline validation, table / JSON / raw output, captcha
  as a file or data URI, cache control.
- Strict typing throughout, shipped with `py.typed`.

[Unreleased]: https://github.com/rahulgurujala/gst-validator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rahulgurujala/gst-validator/releases/tag/v0.1.0
