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
| `search_hsn_codes()` | `/commonservices/hsn/search/qsearch` | no |
| `search_practitioners()` | `/api/search/gstp` | no |
| `search_composition_taxpayers()` | `/api/search/tplist/opteddata` | **yes** |
| `track_application()` | `/trackarn` | **yes** |
| `verify_reference_number()` | `/publicservices/api/verifyRfn` | **yes** |
| `search_temporary_registration()` | `/api/search/smreg` | **yes** |

`goodservice` returns SAC codes for service providers (`bzsdtls`) and HSN
codes for goods (`bzgddtls`); both are parsed into `GoodsOrService`, with
`is_service` telling them apart.

Six endpoints are captcha-gated and five answer freely. See
[Access notes](#access-notes) for what the portal expects of a client.

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

## The other public searches

Each was found by watching the portal's own pages, then confirmed against a
live response unless noted.

**HSN/SAC codes** - `GET /commonservices/hsn/search/qsearch` with
`inputText` and `selectedType`. A `byCode` search takes `category=null` and
covers goods and services together. A `byDesc` search **requires** a category,
`P` for goods or `S` for services: sent without one it answers with an empty
list and no error, which reads as "no matches" rather than a broken call.
Captcha-free and session-free. Answers `{"data":[{"c":code,"n":description}]}`
for goods and services from one endpoint, with no field telling them apart:
`HSNCode.is_service` derives it from the `99` prefix. An unknown code returns
an empty list, not an error.

**GST practitioners** - `POST /api/search/gstp` with
`{searchType, trpNam, stCd, dstCd, pinCd, enrlNo}`. `searchType` is `A` to
search an area and **`B`** to search by enrolment number, not the `E` the
wording suggests; the unused fields must be empty strings rather than nulls,
or the portal answers `FO8001`. The two shapes are **not interchangeable**:
an area search carries no `enrlNo` key at all and nulls the name, and adding
one earns `SWEB_8000`. A pincode narrows a state rather than replacing it; on
its own it is refused with `EM_SRS_FO_016_02`. Captcha-free, and it answers
with a bare JSON list rather than an envelope. **It returns personal data**: a
named individual, a personal mobile number, an email address and a working
address. One unfiltered state query returned 22 people. This package models
the enrolment number, name, category, pincode and address, and deliberately
drops the phone number and email.

**Composition scheme** - `POST /api/search/tplist/opteddata` with
`{op, stcd, fy, captcha}`, where `op` is `O` for opted in and `R` for opted
out. One captcha per state and financial year, single-use like the rest.

A success is a **bare JSON array**, not the `{"status": 1, "data": ...}`
envelope the other list endpoints use; a rejection is an object carrying an
`errorCode`, so the type of the body is what tells them apart. An empty array
means nothing matched that state and year, which is an answer rather than a
failure: Maharashtra for 2025-2026 answered `[]` while 2024-2025 returned
rows.

Each row carries four keys and no more:

```json
{"gstin": "27XXXPX1234X1ZX", "lnm": "A SOLE TRADER",
 "indt": "01/04/2024", "oudt": "31/03/2025"}
```

`lnm` is the legal name, `indt` the date the scheme began applying and `oudt`
the date it stops. There is no trade name, state code or taxpayer type here,
though the state is decodable from the GSTIN. **These are frequently private
individuals**: composition suits small sole proprietors, so `lnm` is often a
person's name rather than a company's.

**Track an application** - `GET /trackarn?arn=&captcha=`. One captcha. An
ARN, Application Reference Number, is the receipt the department issues for
any application: a registration, an amendment, a refund, a cancellation. It
is 15 characters: two fixed letters, the state code, MMYY, a serial and a
check character.

**The success shape here has not been observed.** A real ARN whose
registration had already completed answered `SWEB_10001` with the captcha
accepted, which suggests this pre-login tracker only follows applications
still in progress. The failure path is handled - it raises
`TaxpayerLookupError` carrying the code - but the fields on
`ApplicationStatus` are inferred from the portal's own screens rather than
from a captured response, so treat them as provisional.

**Verify a document reference** - `POST /publicservices/api/verifyRfn` with
`{refId, captcha}`. One captcha. An RFN, Reference Number of the document, is
printed on notices and orders the department issues, and verifying one is how
a recipient tells a genuine notice from a forged one. A reference the portal
does not recognise is an answer, not a failure, so it is reported on
`ReferenceNumber.is_genuine` rather than raised.

**Temporary registration** - `POST /api/search/smreg` with
`{tempId, stateCd, mobNum, captcha}`. Needs either the temporary id or the
registrant's own mobile number, which makes it a self-service lookup rather
than a way to check a third party.

**Advance ruling orders** are **not implemented**. The flow is a captcha gate
at `POST /services/search/arcaptcha` (which answers `{"Status":"1"}` and was
confirmed working with a solved captcha), followed by
`GET /commonservices/ar/orders/search/` taking `terms`, `taxpayerId`,
`legalName`, `orderNo`, `state`, `fromDate`, `toDate` and more. That second
call returns HTTP 400 with an empty message for every parameter combination
tried outside a browser, with the gateway reporting the path as
`//ar/orders/search/`. Rather than ship a method that does not work, it is
written down here for whoever picks it up.

## Captcha-free master lists

Several reference lists need no captcha, session or cookies:

| Endpoint | What it is |
|---|---|
| `/master/allstates?includeCbic=true` | state codes, names, union-territory flags |
| `/master/states` | the same, without CBIC |
| `/master/fyear` | financial years the portal offers |
| `/master/regapp` | application and form type codes |
| `/master/st/{code}/district` | districts within a state |

Only the first is used, by `scripts/check_state_master.py`.

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

**`errorCode`**, each seen live:

| Code | What it meant |
|---|---|
| `SWEB_9000` | a wrong or expired captcha |
| `SWEB_9035` | "Account is Locked", from the portal's published error list |
| `SWEB_8000` | an `enrlNo` key sent on an area practitioner search |
| `FO8001` | an unrecognised `searchType`, such as `E` instead of `B` |
| `EM_SRS_FO_016_02` | "Please enter the mandatory fields": a pincode with no state |
| `RT-NPRFA-1008` | nested under `error`, seen from the financial-year endpoint |
| `SWEB_10001` | seen from `trackarn` with a solved captcha and a real ARN; **meaning unconfirmed** |

Anything else is surfaced as-is on `TaxpayerLookupError.code`. Note that the
first three arrive with HTTP 200, like every other portal rejection.

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
