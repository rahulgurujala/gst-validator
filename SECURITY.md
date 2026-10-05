# Security policy

## Supported versions

The latest release on PyPI is supported.

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/rahulgurujala/gst-validator/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a few days.

## Scope notes

This package talks to the public GST portal on behalf of the person running
it. Two things are worth knowing:

- **Taxpayer data is personal data.** Responses may contain a registered name
  and a place of business. Do not paste real responses into issues, pull
  requests or test fixtures; use a public company's registration or a
  fictional GSTIN.
- **Credentials.** The package stores no credentials and needs none. Releases
  are published with PyPI Trusted Publishing, so no long-lived token exists in
  this repository.
