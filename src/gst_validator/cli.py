"""Command-line entry point: ``gst-validator <GSTIN>``.

Human-facing output is rendered with rich; ``--json`` and ``--raw`` are
written as plain text so the output stays byte-exact for pipes and ``jq``.
Progress messages go to stderr for the same reason.
"""

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from rich.console import Console
from rich.table import Table
from rich.theme import Theme

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

# `out` carries results, `err` carries progress and failures, so that a
# redirected stdout holds nothing but the answer.
out = Console(theme=_THEME, highlight=False)
err = Console(theme=_THEME, highlight=False, stderr=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gst-validator",
        description="Validate a GSTIN and fetch taxpayer details from the GST portal.",
    )
    parser.add_argument("gstin", help="the 15-character GSTIN to look up")
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
        "pan": gstin.pan,
        "entity_type": gstin.entity_type,
        "registration_sequence": gstin.registration_sequence,
    }


def _format(value: object) -> str:
    if isinstance(value, list):
        items: list[Any] = cast(list[Any], value)  # type: ignore[redundant-cast]
        return "\n".join(str(item) for item in items)
    return str(value)


def _status_style(profile: TaxpayerProfile) -> str:
    if profile.details.is_active:
        return "ok"
    return "err" if profile.details.is_cancelled else "warn"


def _profile_table(profile: TaxpayerProfile) -> Table:
    """Two-column table of everything the portal returned, empties dropped."""
    heading = profile.name or profile.gstin
    table = Table(
        title=f"[gstin]{profile.gstin}[/]  {heading}",
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
        if key == "status":
            rendered = f"[{_status_style(profile)}]{rendered}[/]"
        table.add_row(key.replace("_", " "), rendered)
    return table


def _offline_table(gstin: GSTIN) -> Table:
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("field", style="label", no_wrap=True)
    table.add_column("value")
    for key, value in _offline_fields(gstin).items():
        if key in ("gstin", "valid") or value is None:
            continue
        table.add_row(key.replace("_", " "), str(value))
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
            err.print(f"[label]captcha image written to[/] [accent]{written}[/]")
        err.print("[accent]captcha text:[/] ", end="")
        solved = input()
        if args.details_only:
            return TaxpayerProfile(details=client.fetch_details(gstin, solved, refresh=True))
        return client.fetch_profile(gstin, solved, refresh=True)
    finally:
        # The image is single-use: discard it once the text has been read.
        if written is not None and not args.keep_captcha:
            written.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.no_color:
        out.no_color = err.no_color = True

    try:
        gstin = GSTIN.parse(args.gstin)
    except GSTValidatorError as error:
        err.print(f"[err]invalid:[/] {error}")
        return 2

    if args.offline:
        if args.as_json:
            print(json.dumps(_offline_fields(gstin), indent=2))
        else:
            out.print(f"[ok]valid[/] [gstin]{gstin}[/]")
            out.print(_offline_table(gstin))
        return 0

    try:
        with GSTClient() as client:
            cached = None if args.refresh else client.cached(gstin)
            if cached is None:
                profile = _lookup(client, gstin, args)
            elif args.details_only:
                profile = TaxpayerProfile(details=cached)
            else:
                # The details were cached; the extras cost no captcha.
                profile = TaxpayerProfile(
                    details=cached,
                    goods_and_services=client.fetch_goods_and_services(gstin),
                    financial_years=client.fetch_financial_years(gstin),
                    filing_preferences=client.fetch_filing_preferences(gstin),
                )
        if args.raw:
            print(json.dumps(profile.details.raw, indent=2, ensure_ascii=False))
        elif args.as_json:
            print(json.dumps(profile.as_dict(), indent=2, ensure_ascii=False))
        else:
            out.print(_profile_table(profile))
    except GSTValidatorError as error:
        err.print(f"[err]lookup failed:[/] {error}")
        return 1
    except (EOFError, KeyboardInterrupt):
        err.print("[warn]aborted[/]")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
