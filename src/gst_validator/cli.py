"""Command-line entry point: ``gst-validator <GSTIN>``."""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from .client import GSTClient
from .exceptions import GSTValidatorError
from .models import GSTIN, TaxpayerProfile

__all__ = ["main"]


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
    return parser


def _format(value: object) -> str:
    if isinstance(value, list):
        items: list[Any] = cast(list[Any], value)  # type: ignore[redundant-cast]
        return ", ".join(str(item) for item in items)
    return str(value)


def _render(profile: TaxpayerProfile) -> str:
    rows: list[tuple[str, str]] = [
        (key.replace("_", " "), _format(value))
        for key, value in profile.as_dict().items()
        if value not in (None, [], "")
    ]
    width = max((len(name) for name, _ in rows), default=0)
    return "\n".join(f"{name:<{width}}  {value}" for name, value in rows)


def _lookup(client: GSTClient, gstin: GSTIN, args: argparse.Namespace) -> TaxpayerProfile:
    """Fetch a captcha, obtain its text, then look the GSTIN up."""
    captcha = client.fetch_captcha()
    written: Path | None = None
    try:
        if args.captcha_base64:
            print(captcha.data_uri)
        else:
            written = Path(
                args.captcha_path or Path(tempfile.gettempdir()) / f"{gstin}-captcha.png"
            )
            captcha.save(str(written))
            print(f"captcha image written to {written}", file=sys.stderr)
        solved = input("captcha text: ")
        if args.details_only:
            return TaxpayerProfile(details=client.fetch_details(gstin, solved, refresh=True))
        return client.fetch_profile(gstin, solved, refresh=True)
    finally:
        # The image is single-use: discard it once the text has been read.
        if written is not None and not args.keep_captcha:
            written.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        gstin = GSTIN.parse(args.gstin)
    except GSTValidatorError as error:
        print(error, file=sys.stderr)
        return 2

    if args.offline:
        if args.as_json:
            print(
                json.dumps({"gstin": gstin.value, "state_code": gstin.state_code, "pan": gstin.pan})
            )
        else:
            print(f"{gstin} is valid (state {gstin.state_code}, PAN {gstin.pan})")
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
            print(_render(profile))
    except GSTValidatorError as error:
        print(error, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("aborted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
