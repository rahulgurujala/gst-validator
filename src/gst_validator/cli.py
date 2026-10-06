"""Command-line entry point: ``gst-validator <GSTIN>``.

Human-facing output is rendered with rich; ``--json`` and ``--raw`` are
written as plain text so the output stays byte-exact for pipes and ``jq``.
Progress messages go to stderr for the same reason.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, is_dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .bulk import RESULT_KEYS, ValidationResult, enrich_many, validate_many
from .cache import DiskCache, NullCache, TaxpayerCache
from .client import GSTClient
from .exceptions import GSTValidatorError, InvalidPANError
from .gstin import validate_pan
from .models import GSTIN, Registration, TaxpayerProfile
from .search import (
    HSNCode,
)

__all__ = ["main"]

_THEME = Theme(
    {
        "label": "dim",
        "ok": "bold green",
        "warn": "yellow",
        "err": "bold red",
        "gstin": "bold cyan",
        "accent": "cyan",
    }
)

# Portal responses and argv are data, never rich markup: a name or an address
# holding "[/]" would otherwise raise MarkupError and take the CLI down, so
# every dynamic string below is wrapped in Text rather than interpolated into
# a markup string.
#
# `out` carries results, `err` carries progress and failures, so that a
# redirected stdout holds nothing but the answer.
out = Console(theme=_THEME, highlight=False)
err = Console(theme=_THEME, highlight=False, stderr=True)


_EPILOG = """\
examples:
  validate one, no network
    gst-validator 27AAACR5055K1Z7 --offline

  validate a whole column of a spreadsheet, still no network
    gst-validator - --offline --column gstin --format csv < suppliers.csv > checked.csv

  add the portal data that needs no captcha (HSN/SAC codes, years, filing)
    gst-validator - --offline --enrich --format json < gstins.txt

  one full lookup, which asks you to solve a captcha
    gst-validator 27AAACR5055K1Z7

  every registration a company holds, found by its PAN
    gst-validator --pan AAACR5055K

  feed a pipeline
    gst-validator 27AAACR5055K1Z7 --json | jq -r .legal_name

output formats:
  table   rich, human-readable; the default
  json    one object, or an array for several inputs
  jsonl   one object per line, for streaming into jq
  csv     a header row and one row per input, for spreadsheets
  raw     the portal's own response body, unchanged

  Only the table is styled. The rest are plain text, so piping and
  redirection stay byte-exact. Results go to stdout, progress to stderr.

exit codes:
  0 success        1 lookup failed    2 invalid GSTIN
  130 aborted      141 pipe closed by the reader, as head does
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gst-validator",
        description="Validate Indian GSTINs and fetch taxpayer details from the GST portal.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "gstin",
        nargs="*",
        help="one or more 15-character GSTINs, or - to read them from stdin",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('gst-validator')}",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="only validate the GSTIN format and checksum, no captcha lookup",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("table", "json", "jsonl", "csv", "raw"),
        default=None,
        help="how to print the result (default: table)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="shorthand for --format json",
    )
    parser.add_argument(
        "--column",
        metavar="NAME",
        default=None,
        help="read the input as CSV and take GSTINs from this column",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        type=Path,
        default=None,
        help="write the result to a file instead of stdout",
    )
    parser.add_argument(
        "--pan",
        metavar="PAN",
        default=None,
        help="list every GSTIN registered under this PAN (costs one captcha)",
    )
    searches = parser.add_argument_group("other portal searches")
    searches.add_argument(
        "--hsn",
        metavar="TEXT",
        default=None,
        help="look up HSN/SAC codes by code or description (no captcha)",
    )
    searches.add_argument(
        "--by",
        choices=("code", "description"),
        default="code",
        help="how --hsn matches (default: code)",
    )
    searches.add_argument(
        "--services",
        action="store_true",
        help="with --hsn --by description, search service codes instead of goods",
    )
    searches.add_argument(
        "--practitioner",
        action="store_true",
        help="find GST practitioners; narrow with --state, --pincode or --enrolment",
    )
    searches.add_argument("--enrolment", metavar="NO", default=None, help="a practitioner's number")
    searches.add_argument("--state", metavar="CODE", default=None, help="two-digit state code")
    searches.add_argument("--pincode", metavar="PIN", default=None, help="six-digit pincode")
    searches.add_argument(
        "--composition",
        action="store_true",
        help="list composition-scheme taxpayers; needs --state and --year (one captcha)",
    )
    searches.add_argument(
        "--year", metavar="FY", default=None, help="financial year, e.g. 2025-2026"
    )
    searches.add_argument(
        "--opted-out",
        action="store_true",
        help="with --composition, list those who left the scheme instead",
    )
    searches.add_argument(
        "--arn", metavar="ARN", default=None, help="track an application (one captcha)"
    )
    searches.add_argument(
        "--rfn",
        metavar="REF",
        default=None,
        help="verify a document reference number (one captcha)",
    )
    searches.add_argument(
        "--temp-id",
        metavar="ID",
        default=None,
        help="look up a temporary registration (one captcha)",
    )

    parser.add_argument(
        "--enrich",
        action="store_true",
        help="add the captcha-free portal data to each row (HSN/SAC, years, "
        "filing); needs the network, even with --offline",
    )
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="skip the captcha-free extras (HSN/SAC codes, years, filing preferences)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="shorthand for --format raw",
    )
    parser.add_argument(
        "--captcha-path",
        type=Path,
        default=None,
        help="where to write the captcha image (default: a temporary file)",
    )
    parser.add_argument(
        "--captcha-base64",
        action="store_true",
        help="print the captcha as a base64 data URI instead of writing a file",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the on-disk cache of past lookups",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete every cached lookup and exit",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore any cached result for this GSTIN",
    )
    parser.add_argument(
        "--keep-captcha",
        action="store_true",
        help="keep the captcha image on disk after it has been solved",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colour and styling (also honours NO_COLOR)",
    )
    return parser


@contextmanager
def _destination(path: Path | None) -> Generator[None]:
    """Send everything the renderers write to a file instead of stdout.

    Both the plain `print` calls and the rich console are redirected, so every
    format lands in the file and the terminal keeps only progress messages.
    A fresh Console is swapped in rather than the global one being repointed:
    reassigning `out.file` would pin it to whatever stdout happened to be at
    the time, which outlives the block.
    """
    global out
    if path is None:
        yield
        return
    previous = out
    with path.open("w", encoding="utf-8", newline="") as handle:
        out = Console(theme=_THEME, highlight=False, file=handle, no_color=previous.no_color)
        try:
            with redirect_stdout(handle):
                yield
        finally:
            out = previous
    err.print(Text.assemble(("written to ", "label"), (str(path), "accent")))


def _chosen_format(args: argparse.Namespace) -> str:
    """--format wins; --json and --raw stay as the shorthands they always were."""
    if args.format:
        return str(args.format)
    if args.raw:
        return "raw"
    if args.as_json:
        return "json"
    return "table"


def _write_csv(rows: Sequence[dict[str, Any]]) -> None:
    """A header and one row per input, on plain stdout so it pipes cleanly."""
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row.get(key)) for key in columns})


def _csv_cell(value: object) -> str:
    """Flatten a value into one cell without inventing a nested format."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        items: list[Any] = cast("list[Any]", value)  # type: ignore[redundant-cast]
        return "; ".join(str(item) for item in items)
    if isinstance(value, dict):
        # Only `extra` arrives here, and only when the portal has grown a field.
        # A Python dict repr in a spreadsheet cell helps nobody.
        pairs: dict[str, Any] = cast("dict[str, Any]", value)
        return "; ".join(f"{key}={item}" for key, item in pairs.items())
    return str(value)


def _results_table(rows: Sequence[ValidationResult]) -> Table:
    """One line per input, so a batch reads as a list rather than a wall."""
    table = Table(show_header=True, header_style="label", box=None, pad_edge=False)
    table.add_column("gstin", no_wrap=True)
    table.add_column("ok", no_wrap=True)
    table.add_column("state", overflow="fold")
    table.add_column("type", overflow="fold")
    table.add_column("note", overflow="fold")
    for row in rows:
        gstin = row.gstin
        mark = Text("yes", style="ok") if row.is_valid else Text("no", style="err")
        table.add_row(
            Text(gstin.value if gstin else row.value),
            mark,
            Text(gstin.state_name or "" if gstin else ""),
            Text((gstin.registration_type or gstin.layout.value) if gstin else ""),
            Text(row.error or row.enrichment_error or ""),
        )
    return table


def _print_detail(row: ValidationResult) -> None:
    """The vertical view for a single GSTIN, plus anything enrichment added."""
    gstin = row.gstin
    if gstin is None:
        return
    out.print(Text.assemble(("valid ", "ok"), (gstin.value, "gstin")))
    out.print(_offline_table(gstin))
    extras = (
        ("goods and services", [str(item) for item in row.goods_and_services]),
        ("financial years", [year.label for year in row.financial_years]),
        ("filing preferences", [str(item) for item in row.filing_preferences]),
    )
    if any(values for _, values in extras):
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column("field", style="label", no_wrap=True)
        table.add_column("value", overflow="fold")
        for label, values in extras:
            if values:
                table.add_row(label, Text("\n".join(values)))
        out.print(table)


def _emit_results_table(rows: Sequence[ValidationResult]) -> None:
    """Human output: results on stdout, problems on stderr.

    One input gets the detailed view it has always had; a batch gets a line
    each, which is what you want when scanning a column.
    """
    for row in rows:
        if row.gstin is None:
            err.print(Text.assemble(("invalid GSTIN ", "err"), f"{row.value!r}: {row.error}"))
    valid = [row for row in rows if row.gstin is not None]
    if len(valid) == 1 and len(rows) == 1:
        _print_detail(valid[0])
    elif valid:
        out.print(_results_table(valid))


def _emit_results(rows: Sequence[ValidationResult], fmt: str) -> None:
    """Render a bulk offline run in the requested format."""
    payloads = [row.as_dict() for row in rows]
    match fmt:
        case "csv":
            _write_csv(payloads)
        case "jsonl":
            for payload in payloads:
                print(json.dumps(payload, ensure_ascii=False))
        case "json" | "raw":
            single = payloads[0] if len(payloads) == 1 else payloads
            print(json.dumps(single, indent=2, ensure_ascii=False))
        case _:
            _emit_results_table(rows)


def _offline_fields(gstin: GSTIN) -> dict[str, object]:
    """Everything the GSTIN itself encodes, without contacting the portal."""
    return {
        "gstin": gstin.value,
        "valid": True,
        "state_code": gstin.state_code,
        "state_name": gstin.state_name,
        "is_union_territory": gstin.is_union_territory,
        "identifier": gstin.identifier,
        "identifier_type": gstin.identifier_type,
        "pan": gstin.pan,
        "tan": gstin.tan,
        "entity_type": gstin.entity_type,
        "registration_sequence": gstin.registration_sequence,
        "registration_type": gstin.registration_type,
    }


def _format(value: object) -> str:
    if isinstance(value, list):
        items: list[Any] = cast(list[Any], value)  # type: ignore[redundant-cast]
        return "\n".join(str(item) for item in items)
    if isinstance(value, dict):
        # Only `extra` lands here, and only when the portal has grown a field
        # this package does not model yet: worth reading, not a dict repr.
        pairs: dict[str, Any] = cast(dict[str, Any], value)
        return "\n".join(f"{key}: {item}" for key, item in pairs.items())
    return str(value)


def _status_style(profile: TaxpayerProfile) -> str:
    if profile.details.is_active:
        return "ok"
    return "err" if profile.details.is_cancelled else "warn"


def _profile_table(profile: TaxpayerProfile) -> Table:
    """Two-column table of everything the portal returned, empties dropped."""
    heading = profile.name or profile.gstin
    table = Table(
        title=Text.assemble((profile.gstin, "gstin"), "  ", heading),
        title_justify="left",
        show_header=False,
        box=None,
        pad_edge=False,
        padding=(0, 2, 0, 0),
    )
    table.add_column("field", style="label", no_wrap=True)
    table.add_column("value", overflow="fold")

    # The objects render better than their JSON form, and `is_active` only
    # repeats `status`, so the human table takes them from the model.
    overrides: dict[str, str] = {
        "goods_and_services": "\n".join(str(item) for item in profile.goods_and_services),
        "financial_years": "\n".join(str(year) for year in profile.financial_years),
        "filing_preferences": "\n".join(str(item) for item in profile.filing_preferences),
    }
    for key, value in profile.as_dict().items():
        if key == "is_active" or value in (None, [], "", {}):
            continue
        rendered = overrides.get(key) or _format(value)
        style = _status_style(profile) if key == "status" else ""
        table.add_row(key.replace("_", " "), Text(rendered, style=style))
    return table


def _offline_table(gstin: GSTIN) -> Table:
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("field", style="label", no_wrap=True)
    table.add_column("value")
    for key, value in _offline_fields(gstin).items():
        # `is_union_territory` is the only boolean here, and reads as a flag:
        # worth a line when true, noise on every ordinary registration.
        if key in ("gstin", "valid") or value is None or value is False:
            continue
        table.add_row(key.replace("_", " "), Text(str(value)))
    return table


def _lookup_many(gstins: Sequence[GSTIN], args: argparse.Namespace) -> list[TaxpayerProfile]:
    """Full lookups for several GSTINs over one portal session.

    Each lookup still needs its own captcha, because the portal issues a fresh
    one per search, but they share a session rather than opening one each.
    A row that fails is reported and the run carries on.
    """
    profiles: list[TaxpayerProfile] = []
    with GSTClient(cache=_cache_for(args)) as client:
        for index, gstin in enumerate(gstins, start=1):
            if len(gstins) > 1:
                err.print(
                    Text.assemble(
                        ("[", "label"),
                        (f"{index}/{len(gstins)}", "accent"),
                        ("] ", "label"),
                        (gstin.value, "gstin"),
                    )
                )
            cached = None if args.refresh else client.cached(gstin)
            try:
                if cached is not None:
                    profiles.append(
                        TaxpayerProfile(details=cached)
                        if args.details_only
                        else TaxpayerProfile(
                            details=cached,
                            goods_and_services=client.fetch_goods_and_services(gstin),
                            financial_years=client.fetch_financial_years(gstin),
                            filing_preferences=client.fetch_filing_preferences(gstin),
                        )
                    )
                else:
                    profiles.append(_lookup(client, gstin, args))
            except GSTValidatorError as error:
                err.print(Text.assemble(("lookup failed: ", "err"), f"{gstin.value}: {error}"))
    return profiles


@contextmanager
def _solved_captcha(client: GSTClient, args: argparse.Namespace, label: str) -> Generator[str]:
    """Fetch a captcha, hand it over however was asked for, yield the answer.

    Shared by the GSTIN lookup and the PAN search, which differ only in what
    they do with the text. ``label`` names the temporary image file.
    """
    captcha = client.fetch_captcha()
    written: Path | None = None
    try:
        # Everything here is interaction, not the result, so it goes to stderr:
        # stdout must hold only the answer for `--json` and `--raw` to be pipeable.
        if args.captcha_base64:
            err.print(captcha.data_uri, soft_wrap=True, markup=False, highlight=False)
        else:
            written = Path(
                args.captcha_path or Path(tempfile.gettempdir()) / f"{label}-captcha.png"
            )
            captcha.save(written)
            err.print(
                Text.assemble(("captcha image written to ", "label"), (str(written), "accent"))
            )
        err.print(Text("captcha text: ", style="accent"), end="")
        yield input()
    finally:
        # The image is single-use: discard it once the text has been read.
        if written is not None and not args.keep_captcha:
            written.unlink(missing_ok=True)


def _lookup(client: GSTClient, gstin: GSTIN, args: argparse.Namespace) -> TaxpayerProfile:
    """Fetch a captcha, obtain its text, then look the GSTIN up."""
    with _solved_captcha(client, args, gstin.value) as solved:
        if args.details_only:
            return TaxpayerProfile(details=client.fetch_details(gstin, solved, refresh=True))
        return client.fetch_profile(gstin, solved, refresh=True)


def _registrations_table(rows: Sequence[Registration]) -> Table:
    """One line per registration, which is how a PAN result is read."""
    table = Table(show_header=True, header_style="label", box=None, pad_edge=False)
    table.add_column("gstin", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("state", overflow="fold")
    table.add_column("type", overflow="fold")
    for row in rows:
        number = row.number
        style = "ok" if row.is_active else "warn"
        table.add_row(
            Text(row.gstin),
            Text(row.status or "", style=style),
            Text((number.state_name if number else None) or row.state_code or ""),
            Text((number.registration_type if number else None) or ""),
        )
    return table


def _emit_registrations(rows: Sequence[Registration], fmt: str) -> None:
    """Render a PAN result in the requested format."""
    payloads = [row.as_dict() for row in rows]
    match fmt:
        case "csv":
            _write_csv(payloads)
        case "jsonl":
            for payload in payloads:
                print(json.dumps(payload, ensure_ascii=False))
        case "json" | "raw":
            if fmt == "raw":
                # `raw` promises the portal's own body. A registration row keeps
                # none on purpose, so say that rather than pass JSON off as it.
                err.print(
                    Text(
                        "the PAN search keeps no raw body; printing JSON instead",
                        style="warn",
                    )
                )
            print(json.dumps(payloads, indent=2, ensure_ascii=False))
        case _:
            out.print(_registrations_table(rows))


def _run_pan(args: argparse.Namespace, fmt: str) -> int:
    """Look a PAN up and list every registration held under it."""
    try:
        pan = validate_pan(args.pan)
    except InvalidPANError as error:
        # The exception already reads "invalid PAN '...': reason".
        err.print(Text(str(error), style="err"))
        return 2
    # NullCache on purpose: the cache stores taxpayer details, and a PAN
    # result is a different shape entirely, so nothing here would ever read or
    # write it. Handing over the disk cache would only imply otherwise.
    with GSTClient(cache=NullCache()) as client:
        try:
            with _solved_captcha(client, args, pan) as solved:
                rows = client.fetch_registrations_by_pan(pan, solved)
        except GSTValidatorError as error:
            err.print(Text.assemble(("PAN lookup failed: ", "err"), str(error)))
            return 1
    if not rows:
        err.print(Text(f"no registrations found for {pan}", style="warn"))
    _emit_registrations(rows, fmt)
    return 0


def _rows_table(rows: Sequence[Any], columns: Sequence[tuple[str, str]]) -> Table:
    """A plain one-line-per-row table, built from (heading, attribute) pairs."""
    table = Table(show_header=True, header_style="label", box=None, pad_edge=False)
    for heading, _ in columns:
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(*(Text(str(getattr(row, attr, "") or "")) for _, attr in columns))
    return table


def _emit_rows(
    rows: Sequence[Any], fmt: str, columns: Sequence[tuple[str, str]], *, label: str
) -> None:
    """Render any list of dataclass rows in the requested format."""
    payloads = [_as_payload(row) for row in rows]
    match fmt:
        case "csv":
            _write_csv(payloads)
        case "jsonl":
            for payload in payloads:
                print(json.dumps(payload, ensure_ascii=False, default=str))
        case "json" | "raw":
            if fmt == "raw":
                err.print(Text(f"{label} keeps no raw body; printing JSON instead", style="warn"))
            print(json.dumps(payloads, indent=2, ensure_ascii=False, default=str))
        case _:
            if rows:
                out.print(_rows_table(rows, columns))


def _as_payload(row: object) -> dict[str, Any]:
    """Dataclass to a flat JSON-ready dict, dates as ISO strings."""
    if not is_dataclass(row) or isinstance(row, type):
        return {}
    payload = asdict(row)
    # `unmapped` is called `extra` in output, as TaxpayerDetails.as_dict does,
    # and dropped when empty so it is not a blank column on every CSV row.
    unmapped = payload.pop("unmapped", None)
    flat: dict[str, Any] = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in payload.items()
    }
    if unmapped:
        flat["extra"] = unmapped
    return flat


def _run_hsn(args: argparse.Namespace, fmt: str) -> int:
    """HSN/SAC code search. No captcha, so a batch costs nothing."""
    terms = list(_read_gstins([args.hsn]))
    if not terms:
        err.print(Text("no search text given", style="err"))
        return 2
    found: list[HSNCode] = []
    with GSTClient(cache=NullCache()) as client:
        try:
            for term in terms:
                found.extend(client.search_hsn_codes(term, by=args.by, goods=not args.services))
        except GSTValidatorError as error:
            err.print(Text(str(error), style="err"))
            return 1
    if not found:
        err.print(Text("no codes matched", style="warn"))
    _emit_rows(found, fmt, (("code", "code"), ("description", "description")), label="HSN search")
    return 0


def _run_practitioner(args: argparse.Namespace, fmt: str) -> int:
    """GST practitioner directory. No captcha."""
    # An area search is state-first: the portal refuses a pincode on its own
    # with EM_SRS_FO_016_02. An enrolment number stands alone.
    if not (args.enrolment or args.state):
        err.print(
            Text(
                "give --enrolment for one practitioner, or --state "
                "(optionally with --pincode) to search an area",
                style="err",
            )
        )
        return 2
    with GSTClient(cache=NullCache()) as client:
        try:
            rows = client.search_practitioners(
                state_code=args.state, pincode=args.pincode, enrolment_number=args.enrolment
            )
        except GSTValidatorError as error:
            err.print(Text(str(error), style="err"))
            return 1
    if not rows:
        err.print(Text("no practitioners matched", style="warn"))
    _emit_rows(
        rows,
        fmt,
        (("enrolment", "enrolment_number"), ("name", "name"), ("pincode", "pincode")),
        label="the practitioner directory",
    )
    return 0


def _run_composition(args: argparse.Namespace, fmt: str) -> int:
    """Composition-scheme list for one state and year. One captcha."""
    if not (args.state and args.year):
        err.print(Text("--composition needs both --state and --year", style="err"))
        return 2
    with GSTClient(cache=NullCache()) as client:
        try:
            with _solved_captcha(client, args, f"composition-{args.state}") as solved:
                rows = client.search_composition_taxpayers(
                    args.state, args.year, solved, opted_in=not args.opted_out
                )
        except GSTValidatorError as error:
            err.print(Text(str(error), style="err"))
            return 1
    if not rows:
        err.print(Text("no taxpayers listed for that state and year", style="warn"))
    _emit_rows(
        rows,
        fmt,
        (("gstin", "gstin"), ("legal name", "legal_name"), ("type", "taxpayer_type")),
        label="the composition list",
    )
    return 0


def _run_single(args: argparse.Namespace, fmt: str) -> int:
    """The three one-answer searches: ARN, RFN and temporary id. One captcha each."""
    with GSTClient(cache=NullCache()) as client:
        try:
            with _solved_captcha(client, args, "lookup") as solved:
                result: object
                if args.arn:
                    result = client.track_application(args.arn, solved)
                elif args.rfn:
                    result = client.verify_reference_number(args.rfn, solved)
                else:
                    result = client.search_temporary_registration(args.temp_id, solved)
        except GSTValidatorError as error:
            err.print(Text(str(error), style="err"))
            return 1
    payload = _as_payload(result)
    if fmt in ("json", "jsonl", "raw", "csv"):
        if fmt == "csv":
            _write_csv([payload])
        else:
            print(
                json.dumps(
                    payload, indent=None if fmt == "jsonl" else 2, ensure_ascii=False, default=str
                )
            )
    else:
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column("field", style="label", no_wrap=True)
        table.add_column("value", overflow="fold")
        for key, value in payload.items():
            if value not in (None, "", {}, []):
                table.add_row(key.replace("_", " "), Text(str(value)))
        out.print(table)
    return 0


def _read_gstins(values: Sequence[str]) -> Iterator[str]:
    """Yield the GSTINs to process; "-" pulls one per line from stdin."""
    for value in values:
        if value == "-":
            for line in sys.stdin:
                token = line.strip()
                if token:
                    yield token
        else:
            yield value


def _read_csv(column: str) -> tuple[list[str], list[dict[str, str]]]:
    """Take one column of a CSV on stdin, carrying the other fields through.

    A spreadsheet exported from Excel begins with a byte-order mark, which
    would otherwise make the first column "﻿gstin" and never match.
    """
    reader = csv.DictReader(sys.stdin)
    if reader.fieldnames is not None:
        reader.fieldnames = [name.lstrip("﻿") for name in reader.fieldnames]
    if reader.fieldnames is None or column not in reader.fieldnames:
        found = ", ".join(reader.fieldnames or []) or "none"
        message = f"no column {column!r} in the input; found: {found}"
        raise GSTValidatorError(message)
    values: list[str] = []
    extras: list[dict[str, str]] = []
    for record in reader:
        value = (record.get(column) or "").strip()
        if not value:
            continue
        values.append(value)
        # A column of theirs sharing a name with one of ours would otherwise be
        # overwritten silently, so it is carried beside it instead.
        extras.append(
            {
                (f"source_{key}" if key in RESULT_KEYS else key): item
                for key, item in record.items()
                if key != column and item is not None
            }
        )
    return values, extras


def _dump(payload: object, *, compact: bool) -> None:
    """Pretty JSON for a single GSTIN, one object per line for a batch."""
    print(json.dumps(payload, indent=None if compact else 2, ensure_ascii=False))


def _cache_for(args: argparse.Namespace) -> TaxpayerCache:
    """A lookup costs a captcha, so results persist between runs by default."""
    return NullCache() if args.no_cache else DiskCache()


def _collect_inputs(args: argparse.Namespace) -> tuple[list[str], list[dict[str, str]]]:
    """The GSTINs to work on, with any other CSV columns to carry through."""
    if args.column:
        return _read_csv(args.column)
    return list(_read_gstins(args.gstin)), []


def _run_bulk(args: argparse.Namespace, fmt: str) -> int:
    """Offline validation of many inputs, optionally enriched, in one pass."""
    try:
        values, extras = _collect_inputs(args)
    except GSTValidatorError as error:
        err.print(Text.assemble(("input: ", "err"), str(error)))
        return 2
    if args.column and not (args.offline or args.enrich):
        # A column is validated, not looked up: a file of five hundred rows
        # would otherwise mean five hundred captchas. Say so rather than
        # leaving someone to wonder where the taxpayer data went.
        err.print(
            Text(
                "checking format and checksum only; add --enrich for the portal "
                "data that needs no captcha",
                style="label",
            )
        )
    if not values:
        err.print(Text("no GSTIN given", style="err"))
        return 2

    rows: Iterable[ValidationResult] = validate_many(values, extras=extras or None)
    try:
        if args.enrich:
            with GSTClient(cache=_cache_for(args)) as client:
                rows = list(enrich_many(rows, client=client))
        else:
            rows = list(rows)
    except GSTValidatorError as error:
        err.print(Text.assemble(("lookup failed: ", "err"), str(error)))
        return 1

    collected = list(rows)
    _emit_results(collected, fmt)
    return _bulk_exit_code(collected, fmt)


def _bulk_exit_code(rows: Sequence[ValidationResult], fmt: str) -> int:
    """2 if anything failed to parse, 1 if every enrichment failed, else 0."""
    invalid = [row for row in rows if not row.is_valid]
    if invalid:
        # The table renderer has already named each bad row, so a single input
        # needs nothing further; a batch still deserves the tally.
        if fmt != "table" or len(rows) > 1:
            err.print(Text(f"{len(invalid)} of {len(rows)} inputs were invalid", style="err"))
        return 2
    # Enrichment records a failure per row instead of raising, so a run where
    # every lookup failed would otherwise report success. One failure among
    # several is not fatal by design, but none succeeding is a failed run.
    if rows and all(row.enrichment_error for row in rows):
        err.print(Text(f"every lookup failed for all {len(rows)} inputs", style="err"))
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code.

    Takes ``argv`` for testing; reads ``sys.argv`` when it is omitted. Returns
    rather than exits, so it can be called from Python as well as installed as
    the ``gst-validator`` script.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.no_color:
        out.no_color = err.no_color = True
    fmt = _chosen_format(args)

    if args.clear_cache:
        removed = DiskCache().clear()
        err.print(Text(f"cleared {removed} cached lookup(s)", style="label"))
        return 0

    problem = _check_flags(args, parser)
    if problem is not None:
        err.print(Text(problem, style="err"))
        return 2

    try:
        with _destination(args.output):
            return _dispatch(args, fmt)
    except (EOFError, KeyboardInterrupt):
        err.print("[warn]aborted[/]")
        return 130
    except BrokenPipeError:
        # `gst-validator ... | head` closes the pipe early, which is normal and
        # not an error. Python would otherwise print "Exception ignored on
        # flushing sys.stdout" when it flushes at exit, so stdout is pointed at
        # the null device first. 141 is the usual 128 + SIGPIPE.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 141
    except OSError as error:
        err.print(Text.assemble(("could not write output: ", "err"), str(error)))
        return 1


# The mode each flag belongs to. A mode is picked by one of the flags in
# _MODES, or none of them for the ordinary GSTIN lookup, which is `None` here.
# A flag set outside its mode would otherwise be accepted and ignored, which
# reads as the command having done something it did not.
_MODES: Final = ("pan", "hsn", "practitioner", "composition", "arn", "rfn", "temp_id")
_BELONGS_TO: Final[dict[str, tuple[str | None, ...]]] = {
    "offline": (None,),
    "column": (None,),
    "enrich": (None,),
    "details_only": (None,),
    "refresh": (None,),
    "by": ("hsn",),
    "services": ("hsn",),
    "enrolment": ("practitioner",),
    "pincode": ("practitioner",),
    "state": ("practitioner", "composition"),
    "year": ("composition",),
    "opted_out": ("composition",),
}


def _flag(dest: str) -> str:
    """The spelling a reader typed, from the attribute argparse stored it in."""
    return "--" + dest.replace("_", "-")


def _check_flags(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    """Report a mode clash or a flag that does not apply, else ``None``."""
    chosen = [name for name in _MODES if getattr(args, name)]
    if len(chosen) > 1:
        return "pick one of " + ", ".join(_flag(name) for name in chosen)
    mode = chosen[0] if chosen else None
    stray = [
        _flag(dest)
        for dest, modes in _BELONGS_TO.items()
        if getattr(args, dest) != parser.get_default(dest) and mode not in modes
    ]
    if stray:
        where = _flag(mode) if mode else "a plain GSTIN lookup"
        return f"{', '.join(sorted(stray))} does not apply to {where}"
    return None


def _dispatch(args: argparse.Namespace, fmt: str) -> int:
    """Each search is its own mode; offline work goes through the bulk path."""
    if args.pan:
        return _run_pan(args, fmt)
    if args.hsn:
        return _run_hsn(args, fmt)
    if args.practitioner:
        return _run_practitioner(args, fmt)
    if args.composition:
        return _run_composition(args, fmt)
    if args.arn or args.rfn or args.temp_id:
        return _run_single(args, fmt)
    if args.offline or args.column or args.enrich:
        return _run_bulk(args, fmt)

    raws = list(_read_gstins(args.gstin))
    if not raws:
        err.print(Text("no GSTIN given", style="err"))
        return 2

    gstins: list[GSTIN] = []
    worst = 0
    for raw in raws:
        try:
            gstins.append(GSTIN.parse(raw))
        except GSTValidatorError as error:
            # The exception already reads "invalid GSTIN '...': reason".
            err.print(Text(str(error), style="err"))
            worst = 2
    if not gstins:
        return worst

    profiles = _lookup_many(gstins, args)
    if not profiles:
        return max(worst, 1)
    _emit_profiles(profiles, fmt)
    return worst


def _emit_profiles(profiles: Sequence[TaxpayerProfile], fmt: str) -> None:
    """Render full lookups: one object for one input, a list for several."""
    match fmt:
        case "raw":
            bodies = [profile.details.raw for profile in profiles]
            _dump(bodies if len(bodies) != 1 else bodies[0], compact=False)
        case "json":
            rows = [profile.as_dict() for profile in profiles]
            _dump(rows if len(rows) != 1 else rows[0], compact=False)
        case "jsonl":
            for profile in profiles:
                print(json.dumps(profile.as_dict(), ensure_ascii=False))
        case "csv":
            _write_csv([profile.as_dict() for profile in profiles])
        case _:
            for profile in profiles:
                out.print(_profile_table(profile))


if __name__ == "__main__":
    raise SystemExit(main())
