"""Command-line entry point: ``gst-validator <GSTIN>``.

Human-facing output is rendered with rich; ``--json`` and ``--raw`` are
written as plain text so the output stays byte-exact for pipes and ``jq``.
Progress messages go to stderr for the same reason.
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Iterator, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gst-validator",
        description="Validate a GSTIN and fetch taxpayer details from the GST portal.",
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
        "--json",
        action="store_true",
        dest="as_json",
        help="print the modelled fields as JSON instead of a table",
    )
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="skip the captcha-free extras (HSN/SAC codes, years, filing preferences)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="print the portal's response body verbatim, nothing dropped",
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


def _dump(payload: object, *, compact: bool) -> None:
    """Pretty JSON for a single GSTIN, one object per line for a batch."""
    print(json.dumps(payload, indent=None if compact else 2, ensure_ascii=False))


def _cache_for(args: argparse.Namespace) -> TaxpayerCache:
    """A lookup costs a captcha, so results persist between runs by default."""
    return NullCache() if args.no_cache else DiskCache()


def _emit_offline(gstin: GSTIN, args: argparse.Namespace, *, compact: bool) -> None:
    """Report what the number itself encodes, without contacting the portal."""
    if args.as_json:
        _dump(_offline_fields(gstin), compact=compact)
    else:
        out.print(Text.assemble(("valid ", "ok"), (gstin.value, "gstin")))
        out.print(_offline_table(gstin))


def _resolve(client: GSTClient, gstin: GSTIN, args: argparse.Namespace) -> TaxpayerProfile:
    """Return the profile, spending a captcha only when the cache cannot answer."""
    cached = None if args.refresh else client.cached(gstin)
    if cached is None:
        return _lookup(client, gstin, args)
    if args.details_only:
        return TaxpayerProfile(details=cached)
    # The details were cached; the extras cost no captcha.
    return TaxpayerProfile(
        details=cached,
        goods_and_services=client.fetch_goods_and_services(gstin),
        financial_years=client.fetch_financial_years(gstin),
        filing_preferences=client.fetch_filing_preferences(gstin),
    )


def _emit(profile: TaxpayerProfile, args: argparse.Namespace, *, compact: bool) -> None:
    """Write the result: verbatim, as JSON, or as the human table."""
    if args.raw:
        _dump(profile.details.raw, compact=compact)
    elif args.as_json:
        _dump(profile.as_dict(), compact=compact)
    else:
        out.print(_profile_table(profile))


def _one(raw: str, args: argparse.Namespace, *, compact: bool) -> int:
    """Handle a single GSTIN and return its exit code."""
    try:
        gstin = GSTIN.parse(raw)
    except GSTValidatorError as error:
        err.print(Text.assemble(("invalid: ", "err"), str(error)))
        return 2

    if args.offline:
        _emit_offline(gstin, args, compact=compact)
        return 0

    try:
        with GSTClient(cache=_cache_for(args)) as client:
            profile = _resolve(client, gstin, args)
        _emit(profile, args, compact=compact)
    except GSTValidatorError as error:
        err.print(Text.assemble(("lookup failed: ", "err"), str(error)))
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.no_color:
        out.no_color = err.no_color = True

    if args.clear_cache:
        removed = DiskCache().clear()
        err.print(Text(f"cleared {removed} cached lookup(s)", style="label"))
        return 0

    try:
        raws = list(_read_gstins(args.gstin))
    except (EOFError, KeyboardInterrupt):
        err.print("[warn]aborted[/]")
        return 130
    if not raws:
        err.print(Text("no GSTIN given", style="err"))
        return 2

    # One GSTIN keeps the indented JSON object it has always printed; a batch
    # emits JSON Lines so the output streams into jq and friends.
    compact = len(raws) > 1
    worst = 0
    try:
        for raw in raws:
            worst = max(worst, _one(raw, args, compact=compact))
    except (EOFError, KeyboardInterrupt):
        err.print("[warn]aborted[/]")
        return 130
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
