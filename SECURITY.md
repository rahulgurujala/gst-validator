# Security policy

## Supported versions

The latest release on PyPI is supported.

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/rahulgurujala/gst-validator/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a few days.

## Scope notes

This package talks to the public GST portal on behalf of the person running
it. Worth knowing:

- **Taxpayer data is personal data.** Responses may contain a registered name
  and a place of business. Do not paste real responses into issues, pull
  requests or test fixtures; use a public company's registration or a
  fictional GSTIN.
- **The command line caches to disk.** A lookup costs a human-solved captcha,
  so results are kept for 24 hours under the platform cache directory
  (`~/Library/Caches/gst-validator`, `${XDG_CACHE_HOME:-~/.cache}/gst-validator`
  or `%LOCALAPPDATA%`). Those files hold a registered name and place of
  business in plain text. `--no-cache` turns it off and `--clear-cache` empties
  it. The library does not cache to disk unless you pass `DiskCache` yourself.
  A PAN search is not cached at all, so its results never reach the disk.
- **The composition list is personal data.** The scheme suits small sole
  proprietors, so the legal names it returns are frequently individuals
  rather than companies. The fixture for it in this repository is invented.
- **The practitioner directory is personal data.** `search_practitioners()`
  returns named private individuals. The portal publishes a personal phone
  number and email address alongside each one; this package does not carry
  either, and narrowing the search is required rather than optional. Do not
  use it to build a contact list.
- **You can get your own address blocked.** The portal stops answering an
  address that sends too fast, and it takes every endpoint down together.
  The package paces itself, collapses duplicate in-flight requests and backs
  off after a block, but those defaults are per process: a deployment of
  several workers needs a shared limiter. See
  [the deployment guide](docs/deployment.md).
- **Credentials.** The package stores no credentials and needs none. Releases
  are published with PyPI Trusted Publishing, so no long-lived token exists in
  this repository.
