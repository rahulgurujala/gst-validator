# Portal reference

What this package knows about the GST portal, and how it was established.
Everything here was checked against live responses unless it says otherwise;
the portal is undocumented, so a claim without evidence is marked as such.

## Endpoints

| Method | Endpoint | Captcha? |
|---|---|---|
| `fetch_captcha()` | `/services/captcha` | opens the session |
| `fetch_details()` | `/api/search/taxpayerDetails` | **yes**, one per lookup |
| `fetch_goods_and_services()` | `/api/search/goodservice` | no |
| `fetch_financial_years()` | `/api/dropdownfinyear` | no |
| `fetch_filing_preferences()` | `/api/search/taxpayerProfileDetails` | no |
| `fetch_profile()` | all of the above | one |

`goodservice` returns SAC codes for service providers (`bzsdtls`) and HSN
codes for goods (`bzgddtls`); both are parsed into `GoodsOrService`, with
`is_service` telling them apart.

The portal fingerprints clients, so the package sends a browser `User-Agent`
and the `Referer`/`Origin` headers the site expects; without them the captcha
request is reset. For unattended or high-volume use, the official
[GST API](https://developer.gst.gov.in/) through a licensed GSP is the
supported route; this package drives the public, captcha-gated search.

---


## The GSTIN layouts

The portal issues more than one shape. All of them are validated by the same
mod-36 check digit, which is how the two unusual ones were confirmed.

| Layout | Example | Shape |
|---|---|---|
| Ordinary | `27AAACR5055K1Z7` | state, PAN, sequence, type, check |
| TDS deductor | `33AAAGM0289C1DZ` | the same, with `D` at position 14 |
| TCS collector | `27AAICA3918J1CT` | the same, with `C` at position 14 |
| Non-resident (OIDAR) | `9917USA29016OS6` | `99`, year, country, serial, `OS`, check |
| UIN (UN body, embassy) | `2317UNO00001UND` | state, year, `UNO`, serial, `UN`, check |

`GSTIN.layout` tells them apart; see [the library guide](library.md).

### Position 14

`Z` ordinarily. `D` for a tax deductor under section 51 and `C` for a tax
collector under section 52, both confirmed against the portal: Amazon Seller
Services holds `C` registrations beside ordinary ones, and Indian Railways,
NTPC and Indian Oil each hold a `D` registration beside theirs.

Secondary sources also claim `F` for composition dealers and `G` for input
service distributors. **Both look wrong.** Two composition dealers taken from
the portal's own composition list carry a plain `Z`; the scheme shows up in
`dty` instead. The package accepts any letter there, so a real `F` or `G`
would still validate, but it does not label them.

### Positions 3 to 12

A PAN (`AAAAA9999A`) ordinarily. The portal's registration guide says an
applicant without a PAN may register against a TAN (`AAAA99999A`), the letters
and digits the other way round, so that shape is accepted too. **No such
registration has been observed**: every deductor found on the live portal uses
a PAN. `identifier_type` reports which shape is present.

## Field quirks

| What the portal does | What this package does |
|---|---|
| `"NA"`, `""` and `null` all mean absent | All normalise to `None` |
| Dates as `dd/mm/yyyy` | Parsed into `datetime.date` |
| Booleans as `"Yes"` / `"No"` | Parsed into `bool` |
| `pradr` is sometimes one `adr` string, sometimes split fields | Both accepted; `Address.as_line()` returns whichever arrived |
| Errors arrive as HTTP 200 with an `errorCode` | A body with no `gstin` raises `TaxpayerLookupError` |
| Errors sometimes nest under `"error"` | Both shapes read |
| `ctb` is absent for some registrations of a body that has it elsewhere | Degrades to `None` |
| `adhrVdt` appears only when aadhaar is verified | Degrades to `None` |
| A service code is not always six digits | Codes kept verbatim; `00440193` is a pre-GST Service Tax code |

## Values seen live

**`sts`**: `Active`, `Inactive`, `Cancelled suo-moto`. Note the last is not
the bare word "Cancelled", which an equality check would miss, and that
`Inactive` is neither active nor cancelled though it carries a cancellation
date.

**`dty`**: `Regular`, `Composition`, `United Nation Body`.

**`ntcrbs`** (core business activity): `SPO` is "Service Provider and Others"
and `MFT` is "Manufacturer" — note `MFT`, not the `MFR` the name suggests.
The trader code has not been observed, so it is not mapped. An unknown code
passes through unchanged rather than being guessed at.

**`errorCode`**: `SWEB_9000` is a wrong or expired captcha. `SWEB_9035` is
"Account is Locked", from the portal's published error list. Anything else is
surfaced as-is on `TaxpayerLookupError.code`.

## Fields the search endpoint never returns

Observed absent in every live lookup so far, across companies, a bank, a
manufacturer, a statutory body, a cancelled registration and a composition
dealer:

- `adadr`, so `additional_addresses` is always empty
- `lstupdt`, so `last_updated` is always `None`
- `cmpRt`, which answers `"NA"` even for an actual composition dealer

They are modelled because the portal may start sending them, but do not
expect data there.

## Access notes

The portal fingerprints clients: without a browser `User-Agent` and the
`Referer`/`Origin` headers the site expects, the captcha request is reset.
This package sends them.

A captcha is bound to the session that fetched it, so the solved text must be
submitted on the same client instance. Only the taxpayer lookup needs one; the
other three endpoints answer without a captcha, and in testing without cookies
at all.

For unattended or high-volume access, the official
[GST API](https://developer.gst.gov.in/) through a licensed GSP is the
supported route. This package drives the public, captcha-gated search.
