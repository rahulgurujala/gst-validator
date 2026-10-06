"""Command-line entry point: ``gst-validator <GSTIN>``.

Human-facing output is rendered with rich; ``--json`` and ``--raw`` are
written as plain text so the output stays byte-exact for pipes and ``jq``.
Progress messages go to stderr for the same reason.
"""

import argparse
import csv
import json
import sys
import tempfile
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .bulk import ValidationResult, enrich_many, validate_many
from .cache import DiskCache, NullCache, TaxpayerCache
from .client import GSTClient
from .exceptions import GSTValidatorError
from .models import GSTIN, TaxpayerProfile

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

  feed a pipeline
    gst-validator 27AAACR5055K1Z7 --json | jq -r .legal_name

output formats:
  table   rich, human-readable; the default when stdout is a terminal
  json    one object, or an array for several inputs
  jsonl   one object per line, for streaming into jq
  csv     a header row and one row per input, for spreadsheets
  raw     the portal's own response body, unchanged

exit codes:
  0 success   1 lookup failed   2 invalid GSTIN   130 aborted
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
        help="only validate the GSTIN format and checksum, no network call",
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
        "--enrich",
        action="store_true",
        help="add the captcha-free portal data to each row (HSN/SAC, years, filing)",
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


# The keys ValidationResult.as_dict() always produces; an input column sharing
# one of these names is carried through as "source_<name>".
_RESULT_KEYS: Final = frozenset(ValidationResult(value="x").as_dict())


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
        if key in ("gstin", "valid") or value is None:
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


def _lookup(client: GSTClient, gstin: GSTIN, args: argparse.Namespace) -> TaxpayerProfile:
    """Fetch a captcha, obtain its text, then look the GSTIN up."""
    captcha = client.fetch_captcha()
    written: Path | None = None
    try:
        # Everything here is interaction, not the result, so it goes to stderr:
        # stdout must hold only the answer for `--json` and `--raw` to be pipeable.
        if args.captcha_base64:
            err.print(captcha.data_uri, soft_wrap=True, markup=False, highlight=False)
        else:
            written = Path(
                args.captcha_path or Path(tempfile.gettempdir()) / f"{gstin}-captcha.png"
            )
            captcha.save(written)
            err.print(
                Text.assemble(("captcha image written to ", "label"), (str(written), "accent"))
            )
        err.print(Text("captcha text: ", style="accent"), end="")
        solved = input()
        if args.details_only:
            return TaxpayerProfile(details=client.fetch_details(gstin, solved, refresh=True))
        return client.fetch_profile(gstin, solved, refresh=True)
    finally:
        # The image is single-use: discard it once the text has been read.
        if written is not None and not args.keep_captcha:
            written.unlink(missing_ok=True)


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
                (f"source_{key}" if key in _RESULT_KEYS else key): item
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
    """2 if anything failed to parse, with a count on stderr for a batch."""
    invalid = [row for row in rows if not row.is_valid]
    if not invalid:
        return 0
    # The table renderer has already named each bad row, so a single input
    # needs nothing further; a batch still deserves the tally.
    if fmt != "table" or len(rows) > 1:
        err.print(Text(f"{len(invalid)} of {len(rows)} inputs were invalid", style="err"))
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.no_color:
        out.no_color = err.no_color = True
    fmt = _chosen_format(args)

    if args.clear_cache:
        removed = DiskCache().clear()
        err.print(Text(f"cleared {removed} cached lookup(s)", style="label"))
        return 0

    try:
        with _destination(args.output):
            return _dispatch(args, fmt)
    except (EOFError, KeyboardInterrupt):
        err.print("[warn]aborted[/]")
        return 130
    except OSError as error:
        err.print(Text.assemble(("could not write output: ", "err"), str(error)))
        return 1


def _dispatch(args: argparse.Namespace, fmt: str) -> int:
    """Offline work goes through the bulk path; a lookup opens one session."""
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
            err.print(Text.assemble(("invalid GSTIN ", "err"), str(error)))
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
