"""Shared constants, captured payloads and helpers for the test modules."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from gst_validator import GSTClient, TaxpayerCache, TTLCache

# A real, public company registration, used where the fixture below is a
# genuine capture from the portal.
PUBLIC_GSTIN = "27AAACR5055K1Z7"  # Reliance Industries Ltd, Maharashtra
# Fictional but checksum-valid, used wherever a payload had to be invented.
VALID_GSTIN = "27ABCFE1234F1ZW"
PAYLOAD: dict[str, object] = {
    "gstin": VALID_GSTIN,
    "lgnm": "ACME TRADERS",
    "tradeNam": "Acme",
    "sts": "Active",
    "ctb": "Partnership",
    "dty": "Regular",
    "rgdt": "01/07/2017",
    "cxdt": "NA",
    "lstupdt": "12/03/2024",
    "nba": ["Retail Business", "Wholesale Business"],
    "ctj": "RANGE-IV",
    "ctjCd": "ZT0303",
    "stj": "Mumbai",
    "stjCd": "MUM-VAT",
    "einvoiceStatus": "Yes",
    "pradr": {
        "addr": {
            "flno": "2nd Floor",
            "bno": "12",
            "bnm": "Sai Plaza",
            "st": "MG Road",
            "loc": "Andheri",
            "city": "Mumbai",
            "dst": "Mumbai Suburban",
            "stcd": "Maharashtra",
            "pncd": "400058",
        },
        "ntr": "Retail Business, Warehouse",
    },
    "adadr": [
        {
            "addr": {"bnm": "Unit 9", "st": "Link Road", "stcd": "Maharashtra", "pncd": "400053"},
            "ntr": "Warehouse",
        }
    ],
}


GOODS_PAYLOAD: dict[str, object] = {
    "bzsdtls": [{"saccd": "998314", "sdes": "Information technology design services"}]
}
FINYEAR_PAYLOAD: dict[str, object] = {
    "status": 1,
    "data": [{"year": "2025-2026", "value": "2025"}, {"year": "2026-2027", "value": "2026"}],
}
# A real capture: Reliance Industries' registrations under one PAN, as the PAN
# search returned them. The live response held 68; seven are kept here, chosen
# to cover both statuses and two union territories. Public corporate
# registrations, not anyone's personal data.
PAN = "AAACR5055K"
REGISTRATIONS_PAYLOAD: dict[str, object] = json.loads(
    (Path(__file__).parent / "fixtures" / "registrations_by_pan.json").read_text()
)
PROFILE_PAYLOAD: dict[str, object] = {
    "status": 1,
    "data": {
        "response": [{"quarter": "Q1", "preference": "Q"}, {"quarter": "Q2", "preference": "M"}]
    },
}


def transport(details: httpx.Response | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            case "/services/captcha":
                return httpx.Response(
                    200, content=b"\x89PNG-bytes", headers={"content-type": "image/png"}
                )
            case "/services/api/search/taxpayerDetails":
                return details or httpx.Response(200, json=PAYLOAD)
            case "/services/api/search/goodservice":
                return httpx.Response(200, json=GOODS_PAYLOAD)
            case "/services/api/dropdownfinyear":
                return httpx.Response(200, json=FINYEAR_PAYLOAD)
            case "/services/api/search/taxpayerProfileDetails":
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            case "/services/api/get/gstndtls":
                return httpx.Response(200, json=REGISTRATIONS_PAYLOAD)
            case _:  # pragma: no cover - guards against path typos
                return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeClient(GSTClient):
    """GSTClient pinned to a mock transport, usable where the CLI builds one."""

    def __init__(self, transport: httpx.MockTransport, cache: TaxpayerCache | None = None) -> None:
        super().__init__(transport=transport, cache=cache or TTLCache())


def client_factory(
    transport: httpx.MockTransport, cache: TaxpayerCache | None = None
) -> Callable[..., GSTClient]:
    """Stand in for `gst_validator.cli.GSTClient`, which the CLI calls with a cache."""

    def build(**kwargs: object) -> GSTClient:
        chosen = cache if cache is not None else kwargs.get("cache")
        return FakeClient(transport, cache=chosen if isinstance(chosen, TaxpayerCache) else None)

    return build


def answer(text: str) -> Callable[..., str]:
    """Stand-in for ``input`` that always returns ``text``."""

    def prompt(*_args: object, **_kwargs: object) -> str:
        return text

    return prompt
