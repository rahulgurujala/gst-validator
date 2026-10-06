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
| `fetch_registrations_by_pan()` | `/api/get/gstndtls` | **yes**, one per PAN |

`goodservice` returns SAC codes for service providers (`bzsdtls`) and HSN
codes for goods (`bzgddtls`); both are parsed into `GoodsOrService`, with
`is_service` telling them apart.

Two endpoints are captcha-gated, the taxpayer lookup and the PAN search; the
other three answer freely. See [Access notes](#access-notes) for what the
portal expects of a client.

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

## The PAN search

`POST /services/api/get/gstndtls` with `{"panNO": "AAACR5055K", "captcha": "..."}`
answers with every GSTIN registered against that PAN:

```json
{"panNum": null, "action": null, "gstinResList": [
  {"gstin": "24AAACR5055K2ZC", "authStatus": "Inactive", "stateCd": "24"}]}
```

Established against the live portal:

- The captcha is **single-use**. Replaying a solved one on the same session
  for a second PAN is refused with `SWEB_9000`, exactly as the taxpayer
  lookup refuses it.
- `panNum` and `action` came back `null`, which is why nothing models them
  and the client returns the list rather than an envelope object.
- A rejection carries no `gstinResList` at all, so its absence - not the
  status code - is what marks a failed lookup, the same rule as `gstin` on
  the taxpayer endpoint.
- `stateCd` repeated the number's own first two characters in all 68 rows of
  a live response. It is kept as sent rather than derived, so a future
  disagreement stays visible instead of one side silently winning.
- A large taxpayer holds far more registrations than states: Reliance returned
  68 against 38 state codes, several states more than once, because a company
  may hold multiple registrations in one state. `registration_sequence` (the
  13th character) tells them apart.
- The request needs `Referer: .../searchtpbypan`; the portal fingerprints
  clients per page.

## Blind endpoint discovery does not work

The portal sits behind a WAF that rejects any path not on its allowlist with
an HTML `Request Rejected` page and a support ID, **not** a 404, while known
paths on the same session keep answering normally. Guessing endpoint names
therefore tells you nothing. Every endpoint here was found by watching what
the portal's own pages call.

## State and union territory master

`GET /master/allstates?includeCbic=true` needs no captcha, no session and no
cookies, and returns the portal's own list:

```json
{"data": [{"c": "27", "n": "Maharashtra", "u": "N", "m": "M2"}]}
```

`u` flags a union territory. Six codes carry it: 04, 25, 26, 31, 35 and 38.
Delhi (07) and Puducherry (34) do **not**, although both are union
territories: each has a legislature and is treated as a state for GST.

The package decodes states offline, so this is not wrapped as a method. It
backs `scripts/check_state_master.py`, which diffs the hardcoded table against
the portal and is run before a release. The table was last checked against it
with no drift. Two codes are kept that this master omits: `28`, retired on the
Telangana split but still carried by older registrations, and `96`, from the
NIC e-invoice master codes. This dropdown labels `99` "CBIC"; in a GSTIN it is
the non-resident prefix, which is what the package reports.

## Field quirks

| What the portal does | What this package does |
|---|---|
| `"NA"`, `""` and `null` all mean absent | All normalise to `None` |
| Dates as `dd/mm/yyyy` | Parsed into `datetime.date` |
| Booleans as `"Yes"` / `"No"` | Parsed into `bool`; `"NA"` and `""` become `None`, not `False` |
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
and `MFT` is "Manufacturer" - note `MFT`, not the `MFR` the name suggests.
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
submitted on the same client instance, and is single-use: a second lookup, or
a second PAN, needs a fresh one. The taxpayer lookup and the PAN search are
the two that need one; the other three endpoints answer without a captcha, and
in testing without cookies at all.

For unattended or high-volume access, the official
[GST API](https://developer.gst.gov.in/) through a licensed GSP is the
supported route. This package drives the public, captcha-gated search.
