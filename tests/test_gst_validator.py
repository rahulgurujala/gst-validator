"""Offline tests: every HTTP call goes through httpx.MockTransport."""

import argparse
import asyncio
import io
import json
import subprocess
import sys
import time
from collections.abc import Callable, Coroutine
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

import gst_validator
from gst_validator import (
    GSTIN,
    Address,
    AsyncGSTClient,
    Captcha,
    CaptchaError,
    DiskCache,
    GSTClient,
    InvalidGSTINError,
    NullCache,
    TaxpayerCache,
    TaxpayerDetails,
    TaxpayerLookupError,
    TaxpayerProfile,
    TTLCache,
)
from gst_validator.cli import main

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
PROFILE_PAYLOAD: dict[str, object] = {
    "status": 1,
    "data": {
        "response": [{"quarter": "Q1", "preference": "Q"}, {"quarter": "Q2", "preference": "M"}]
    },
}


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests off both shared caches: the process-wide one and the disk."""
    monkeypatch.setattr("gst_validator.client.DEFAULT_CACHE", TTLCache())
    monkeypatch.setattr(
        "gst_validator.cache.DiskCache.default_directory",
        staticmethod(lambda: tmp_path / "disk-cache"),
    )


def _transport(details: httpx.Response | None = None) -> httpx.MockTransport:
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
            case _:  # pragma: no cover - guards against path typos
                return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestGSTIN:
    def test_parses_and_exposes_parts(self) -> None:
        gstin = GSTIN.parse(f"  {VALID_GSTIN.lower()} ")
        assert gstin.value == VALID_GSTIN
        assert gstin.state_code == "27"
        assert gstin.pan == "ABCFE1234F"

    def test_rejects_bad_format(self) -> None:
        with pytest.raises(InvalidGSTINError, match="format"):
            GSTIN("27ABCFE1234F1Z")

    def test_rejects_bad_checksum(self) -> None:
        with pytest.raises(InvalidGSTINError, match="checksum"):
            GSTIN(VALID_GSTIN[:-1] + "A")

    def test_is_hashable_and_frozen(self) -> None:
        assert len({GSTIN(VALID_GSTIN), GSTIN.parse(VALID_GSTIN)}) == 1


class TestCaptcha:
    def test_data_uri(self) -> None:
        assert Captcha(b"ab").data_uri == "data:image/png;base64,YWI="


class TestGSTClient:
    def test_fetch_captcha(self) -> None:
        with GSTClient(transport=_transport()) as client:
            captcha = client.fetch_captcha()
        assert captcha.content == b"\x89PNG-bytes"
        assert captcha.data_uri.startswith("data:image/png;base64,")

    def test_fetch_details(self) -> None:
        with GSTClient(transport=_transport()) as client:
            details = client.fetch_details(VALID_GSTIN, " 1a2b3 ")
        assert details.legal_name == "ACME TRADERS"
        assert details.constitution == "Partnership"
        assert details.is_active
        assert not details.is_cancelled

    def test_empty_captcha_text_rejected(self) -> None:
        with GSTClient(transport=_transport()) as client, pytest.raises(TaxpayerLookupError):
            client.fetch_details(VALID_GSTIN, "   ")

    def test_portal_error_message_raised(self) -> None:
        error = httpx.Response(200, json={"errorMsg": "Invalid captcha"})
        with (
            GSTClient(transport=_transport(error)) as client,
            pytest.raises(TaxpayerLookupError, match="Invalid captcha"),
        ):
            client.fetch_details(VALID_GSTIN, "wrong")

    def test_error_code_without_message_explained(self) -> None:
        """The portal rejects with HTTP 200, a null message and only a code."""
        rejected = httpx.Response(200, json={"url": "/", "message": None, "errorCode": "SWEB_9000"})
        with GSTClient(transport=_transport(rejected)) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha") as caught:
                client.fetch_details(VALID_GSTIN, "702603")
        assert caught.value.code == "SWEB_9000"

    def test_unknown_error_code_still_raises(self) -> None:
        rejected = httpx.Response(200, json={"errorCode": "SWEB_1234"})
        with GSTClient(transport=_transport(rejected)) as client:
            with pytest.raises(TaxpayerLookupError, match="no taxpayer details"):
                client.fetch_details(VALID_GSTIN, "702603")

    def test_http_error_wrapped(self) -> None:
        with (
            GSTClient(transport=_transport(httpx.Response(503))) as client,
            pytest.raises(TaxpayerLookupError),
        ):
            client.fetch_details(VALID_GSTIN, "1a2b3")

    def test_non_image_captcha_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="maintenance", headers={"content-type": "text/html"})

        with (
            GSTClient(transport=httpx.MockTransport(handler)) as client,
            pytest.raises(CaptchaError),
        ):
            client.fetch_captcha()


class TestCLI:
    def test_offline_validation(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["pan"] == "ABCFE1234F"

    def test_invalid_gstin_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["NOPE", "--offline"]) == 2
        assert "invalid GSTIN" in capsys.readouterr().err


class TestTaxpayerDetails:
    """Parsing of the portal payload into the structured object."""

    @pytest.fixture
    def details(self) -> TaxpayerDetails:
        return TaxpayerDetails.from_payload(dict(PAYLOAD))

    def test_scalar_fields(self, details: TaxpayerDetails) -> None:
        assert details.name == "Acme"  # trade name wins over legal name
        assert details.taxpayer_type == "Regular"
        assert details.einvoice_enabled is True
        assert str(details) == "Acme (Active)"

    def test_dates_parsed(self, details: TaxpayerDetails) -> None:
        assert details.registration_date == date(2017, 7, 1)
        assert details.last_updated == date(2024, 3, 12)
        assert details.cancellation_date is None  # portal sent the string "NA"

    def test_addresses(self, details: TaxpayerDetails) -> None:
        assert details.principal_address is not None
        assert details.principal_address.pincode == "400058"
        assert details.principal_address.nature_of_business == ("Retail Business", "Warehouse")
        assert "Sai Plaza, MG Road" in details.principal_address.as_line()
        assert len(details.addresses) == 2

    def test_jurisdictions(self, details: TaxpayerDetails) -> None:
        assert details.central_jurisdiction.is_known
        assert str(details.state_jurisdiction) == "Mumbai (MUM-VAT)"

    def test_number_and_raw(self, details: TaxpayerDetails) -> None:
        assert details.number == GSTIN(VALID_GSTIN)
        assert details.raw["ctjCd"] == "ZT0303"

    def test_as_dict_is_json_ready(self, details: TaxpayerDetails) -> None:
        payload = json.dumps(details.as_dict())
        assert '"registration_date": "2017-07-01"' in payload

    def test_unknown_core_business_code_passes_through(self) -> None:
        """Better a raw code than a confidently wrong expansion."""
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN, "ntcrbs": "ZZZ"})
        assert details.core_business_activity == "ZZZ"

    def test_missing_keys_tolerated(self) -> None:
        bare = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN})
        assert bare.name is None
        assert bare.addresses == ()
        assert not bare.central_jurisdiction.is_known

    def test_empty_address_renders_empty(self) -> None:
        assert Address.from_payload({}).as_line() == ""


class TestGSTINExtras:
    def test_decodes_state_and_entity(self) -> None:
        gstin = GSTIN(VALID_GSTIN)
        assert gstin.state_name == "Maharashtra"
        assert gstin.entity_type == "Firm / LLP"
        assert gstin.registration_sequence == "1"

    def test_is_valid_does_not_raise(self) -> None:
        assert GSTIN.is_valid(VALID_GSTIN)
        assert not GSTIN.is_valid("NOPE")


class TestCache:
    def test_hit_skips_the_network(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("taxpayerDetails"):
                calls += 1
            return httpx.Response(200, json=PAYLOAD)

        cache = TTLCache()
        transport = httpx.MockTransport(handler)
        with GSTClient(transport=transport, cache=cache) as client:
            first = client.fetch_details(VALID_GSTIN, "1a2b3")
            second = client.fetch_details(VALID_GSTIN, "ignored")
            assert client.cached(VALID_GSTIN) == first
            assert client.fetch_details(VALID_GSTIN, "1a2b3", refresh=True) is not None
        assert first == second
        assert calls == 2  # one cold lookup, one forced refresh

    def test_entries_expire(self) -> None:
        now = 1000.0
        cache = TTLCache(ttl=60, clock=lambda: now)
        cache.set(VALID_GSTIN, TaxpayerDetails(gstin=VALID_GSTIN))
        assert VALID_GSTIN in cache
        now += 61
        assert cache.get(VALID_GSTIN) is None
        assert len(cache) == 0

    def test_lru_eviction(self) -> None:
        cache = TTLCache(maxsize=2)
        for suffix in "abc":
            cache.set(suffix, TaxpayerDetails(gstin=suffix))
        assert len(cache) == 2
        assert cache.get("a") is None

    def test_rejects_bad_configuration(self) -> None:
        with pytest.raises(ValueError):
            TTLCache(ttl=0)
        with pytest.raises(ValueError):
            TTLCache(maxsize=0)

    def test_null_cache_disables_caching(self) -> None:
        cache = NullCache()
        cache.set(VALID_GSTIN, TaxpayerDetails(gstin=VALID_GSTIN))
        assert cache.get(VALID_GSTIN) is None


class TestLivePayload:
    """Parsed against a real portal response shape (captured 2026-10-05).

    The identifying values were replaced with fictional ones; the keys, the
    date formats and the quirks ("NA", "", the flat ``adr`` address) are
    exactly what the portal sent.

    The portal sends the principal address as a single ``adr`` string, not the
    split ``bnm``/``st``/``pncd`` fields, so this guards that shape.
    """

    @pytest.fixture
    def live(self) -> TaxpayerDetails:
        path = Path(__file__).parent / "fixtures" / "taxpayer_live.json"
        return TaxpayerDetails.from_payload(json.loads(path.read_text()))

    def test_address_from_flat_string(self, live: TaxpayerDetails) -> None:
        assert live.principal_address is not None
        assert not live.principal_address.is_empty
        assert live.principal_address.as_line().startswith("ROOM NO.1, EXAMPLE NAGAR")

    def test_every_portal_key_is_modelled(self, live: TaxpayerDetails) -> None:
        assert live.unmapped == {}

    def test_extra_fields(self, live: TaxpayerDetails) -> None:
        # "SPO" is the portal's Core Business Activity code, a different field
        # from `nba` ("Supplier of Services") that sits beside it in the payload.
        assert live.core_business_activity == "Service Provider and Others"
        assert live.nature_of_business == ("Supplier of Services",)
        assert live.aadhaar_verified is True
        assert live.aadhaar_verified_on == date(2025, 9, 22)
        assert live.ekyc_status == "Not Applicable"
        assert live.composition_rate is None  # portal sent "NA"
        assert live.einvoice_enabled is False

    def test_empty_cancellation_date(self, live: TaxpayerDetails) -> None:
        assert live.cancellation_date is None  # portal sent ""
        assert live.is_active


class TestCaptchaCleanup:
    def test_image_is_deleted_after_solving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "captcha.png"
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target)]) == 0
        assert not target.exists()
        assert "ACME TRADERS" in capsys.readouterr().out

    def test_keep_captcha_retains_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "captcha.png"
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target), "--keep-captcha"]) == 0
        assert target.read_bytes() == b"\x89PNG-bytes"


def _client_factory(
    transport: httpx.MockTransport, cache: TaxpayerCache | None = None
) -> Callable[..., GSTClient]:
    """Stand in for `gst_validator.cli.GSTClient`, which the CLI calls with a cache."""

    def build(**kwargs: object) -> GSTClient:
        chosen = cache if cache is not None else kwargs.get("cache")
        return _FakeClient(transport, cache=chosen if isinstance(chosen, TaxpayerCache) else None)

    return build


def _answer(text: str) -> Callable[..., str]:
    """Stand-in for ``input`` that always returns ``text``."""

    def prompt(*_args: object, **_kwargs: object) -> str:
        return text

    return prompt


class _FakeClient(GSTClient):
    """GSTClient pinned to a mock transport, usable where the CLI builds one."""

    def __init__(self, transport: httpx.MockTransport, cache: TaxpayerCache | None = None) -> None:
        super().__init__(transport=transport, cache=cache or TTLCache())


class TestCaptchaFreeEndpoints:
    """goodservice, dropdownfinyear and taxpayerProfileDetails need no captcha."""

    def test_goods_entries_are_parsed_too(self) -> None:
        """Goods taxpayers answer with ``bzgddtls``/``hsncd``, not ``bzsdtls``/``saccd``."""
        live = json.loads((Path(__file__).parent / "fixtures" / "goods_live.json").read_text())
        # Captured verbatim from the portal for PUBLIC_GSTIN - no captcha needed.

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=live)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            items = client.fetch_goods_and_services(VALID_GSTIN)
        assert len(items) == 5
        assert items[0].code == "55151190"
        assert not items[0].is_service  # HSN, so goods

    def test_mixed_goods_and_services(self) -> None:
        mixed = {
            "bzsdtls": [{"saccd": "998314", "sdes": "IT services"}],
            "bzgddtls": [{"hsncd": "39269080", "gdes": "Polypropylene articles"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=mixed)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            items = client.fetch_goods_and_services(VALID_GSTIN)
        assert [item.is_service for item in items] == [True, False]

    def test_goods_and_services(self) -> None:
        with GSTClient(transport=_transport()) as client:
            items = client.fetch_goods_and_services(VALID_GSTIN)
        assert len(items) == 1
        assert items[0].code == "998314"
        assert items[0].is_service
        assert str(items[0]).startswith("998314 - Information")

    def test_financial_years(self) -> None:
        with GSTClient(transport=_transport()) as client:
            years = client.fetch_financial_years(VALID_GSTIN)
        assert [year.label for year in years] == ["2025-2026", "2026-2027"]
        assert years[0].start_year == 2025

    def test_filing_preferences(self) -> None:
        with GSTClient(transport=_transport()) as client:
            preferences = client.fetch_filing_preferences(VALID_GSTIN)
        assert preferences[0].is_quarterly
        assert preferences[1].is_monthly
        assert str(preferences[0]) == "Q1: quarterly"

    def test_profile_bundles_everything(self) -> None:
        with GSTClient(transport=_transport()) as client:
            profile = client.fetch_profile(VALID_GSTIN, "1a2b3")
        assert profile.name == "Acme"
        assert profile.is_active
        assert len(profile.goods_and_services) == 1
        assert len(profile.financial_years) == 2
        assert len(profile.filing_preferences) == 2
        payload = profile.as_dict()
        assert payload["goods_and_services"][0]["code"] == "998314"
        assert payload["financial_years"] == ["2025-2026", "2026-2027"]
        assert payload["legal_name"] == "ACME TRADERS"  # details still included

    def test_session_is_opened_once(self) -> None:
        visits = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal visits
            if request.url.path == "/services/searchtp":
                visits += 1
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=GOODS_PAYLOAD)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.fetch_goods_and_services(VALID_GSTIN)
            client.fetch_goods_and_services(VALID_GSTIN)
        assert visits == 1

    def test_envelope_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json={"status": 0, "errorCode": "SWEB_9035"})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TaxpayerLookupError, match="locked") as caught:
                client.fetch_financial_years(VALID_GSTIN)
        assert caught.value.code == "SWEB_9035"

    def test_unknown_error_code_is_not_given_an_invented_meaning(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json={"status": 0, "errorCode": "SWEB_4242"})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            # Surfaced as-is; the caller can still branch on `.code`.
            with pytest.raises(TaxpayerLookupError, match="SWEB_4242") as caught:
                client.fetch_financial_years(VALID_GSTIN)
        assert caught.value.code == "SWEB_4242"

    def test_invalid_gstin_never_reaches_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be called")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(InvalidGSTINError):
                client.fetch_goods_and_services("NOPE")


class TestAsyncClient:
    def test_profile_matches_the_sync_client(self) -> None:
        """httpx.MockTransport serves async clients too, so no extra plugin."""

        async def run() -> TaxpayerProfile:
            async with AsyncGSTClient(transport=_transport(), cache=TTLCache()) as client:
                return await client.fetch_profile(VALID_GSTIN, "1a2b3")

        profile = asyncio.run(run())
        assert profile.gstin == VALID_GSTIN
        assert profile.details.legal_name == "ACME TRADERS"
        assert len(profile.goods_and_services) == 1
        assert len(profile.filing_preferences) == 2


class TestPackaging:
    """Guards that the release machinery cannot silently drift."""

    def test_dunder_version_matches_the_installed_distribution(self) -> None:
        """release-please bumps both; an edit to the marker would desync them."""
        assert gst_validator.__version__ == metadata.version("gst-validator")

    def test_everything_in_dunder_all_is_importable(self) -> None:
        missing = [name for name in gst_validator.__all__ if not hasattr(gst_validator, name)]
        assert missing == []

    def test_module_entry_point_runs(self) -> None:
        """`python -m gst_validator` is documented, so it must work."""
        result = subprocess.run(
            [sys.executable, "-m", "gst_validator", VALID_GSTIN, "--offline", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["valid"] is True


class TestOfflineOutput:
    def test_json_carries_everything_the_number_encodes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "gstin": VALID_GSTIN,
            "valid": True,
            "state_code": "27",
            "state_name": "Maharashtra",
            "identifier": "ABCFE1234F",
            "identifier_type": "PAN",
            "pan": "ABCFE1234F",
            "tan": None,
            "entity_type": "Firm / LLP",
            "registration_sequence": "1",
            "registration_type": "Regular",
        }


class TestCaptchaSave:
    def test_accepts_both_str_and_path(self, tmp_path: Path) -> None:
        captcha = Captcha(b"\x89PNG-bytes")
        as_path = tmp_path / "from-path.png"
        as_str = tmp_path / "from-str.png"
        captcha.save(as_path)
        captcha.save(str(as_str))
        assert as_path.read_bytes() == as_str.read_bytes() == b"\x89PNG-bytes"


class TestRichOutput:
    """The human output is styled; the machine output must stay byte-exact."""

    def test_json_output_has_no_styling_or_wrapping(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--json"]) == 0
        stdout = capsys.readouterr().out
        assert "\x1b[" not in stdout  # no ANSI escapes
        assert json.loads(stdout)["legal_name"] == "ACME TRADERS"

    def test_raw_output_round_trips(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--raw"]) == 0
        assert json.loads(capsys.readouterr().out) == PAYLOAD

    def test_table_renders_objects_not_dicts(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "998314 - Information technology design services" in stdout
        assert "Q1: quarterly" in stdout
        assert "{'code'" not in stdout  # never the repr of a dict
        assert "is active" not in stdout  # redundant with `status`

    def test_offline_table_lists_the_decoded_parts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "valid" in stdout
        assert "Maharashtra" in stdout
        assert "Firm / LLP" in stdout

    def test_prompt_never_lands_on_stdout(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real `input()` writes its prompt to stdout, which would corrupt --json.

        The stub here mimics that, so the test fails if the prompt is ever
        passed to `input()` again instead of being printed to stderr.
        """

        def prompting_input(prompt: str = "") -> str:
            sys.stdout.write(prompt)
            return "1a2b3"

        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", prompting_input)
        assert main([VALID_GSTIN, "--json"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN
        assert "captcha text" in captured.err

    def test_captcha_data_uri_goes_to_stderr(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--captcha-base64 with --json must leave stdout as pure JSON."""
        monkeypatch.setattr("gst_validator.cli.GSTClient", _client_factory(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--json", "--captcha-base64"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN
        assert "data:image/png;base64," in captured.err

    def test_errors_go_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["NOPE", "--offline"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "invalid GSTIN" in captured.err


class TestUnmappedRendering:
    def test_extra_fields_render_as_pairs(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A portal change surfaces in `extra`; it should read, not be a repr."""
        grown = dict(PAYLOAD) | {"newField": "surprise"}

        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=grown)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            _client_factory(httpx.MockTransport(handler)),
        )
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "newField: surprise" in stdout
        assert "{'newField'" not in stdout


class TestMarkupSafety:
    """Portal responses and argv are data, never rich markup."""

    def test_square_brackets_in_an_argument_do_not_crash(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`[/]` is a closing tag to rich: interpolating it raises MarkupError."""
        assert main(["[/]", "--offline"]) == 2
        assert "invalid GSTIN" in capsys.readouterr().err

    def test_markup_in_portal_data_is_shown_literally(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hostile = dict(PAYLOAD)
        hostile["lgnm"] = "ACME [/] [bold red]INJECTED[/] TRADERS"

        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=hostile)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            _client_factory(httpx.MockTransport(handler)),
        )
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "[bold red]INJECTED[/]" in stdout  # printed, not interpreted


class TestNestedErrorEnvelope:
    """Some endpoints nest the error fields under "error" instead of inlining them."""

    def test_nested_code_and_message_are_surfaced(self) -> None:
        nested = {
            "status": 0,
            "error": {"url": "/", "message": "Invalid paramater", "errorCode": "RT-NPRFA-1008"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=nested)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TaxpayerLookupError, match="Invalid paramater") as caught:
                client.fetch_financial_years(VALID_GSTIN)
        assert caught.value.code == "RT-NPRFA-1008"

    def test_flat_envelope_still_works(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json={"status": 0, "errorCode": "SWEB_9035"})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TaxpayerLookupError, match="locked") as caught:
                client.fetch_financial_years(VALID_GSTIN)
        assert caught.value.code == "SWEB_9035"


class TestDiskCache:
    """The CLI is short-lived, so its cache has to outlive the process."""

    def test_round_trips_through_a_new_instance(self, tmp_path: Path) -> None:
        details = TaxpayerDetails.from_payload(dict(PAYLOAD))
        DiskCache(directory=tmp_path).set(VALID_GSTIN, details)
        # A different instance stands in for the next CLI invocation.
        restored = DiskCache(directory=tmp_path).get(VALID_GSTIN)
        assert restored is not None
        assert restored.legal_name == "ACME TRADERS"
        assert restored.raw == PAYLOAD  # the portal body is what is stored

    def test_entries_expire(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path, ttl=0.01)
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))
        time.sleep(0.02)
        assert cache.get(VALID_GSTIN) is None

    def test_corrupt_and_missing_entries_are_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        assert cache.get(VALID_GSTIN) is None  # nothing written yet
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / f"{VALID_GSTIN}.json").write_text("{not json")
        assert cache.get(VALID_GSTIN) is None

    def test_unwritable_directory_does_not_fail_the_lookup(self, tmp_path: Path) -> None:
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("")
        cache = DiskCache(directory=blocked / "sub")
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))  # must not raise
        assert cache.get(VALID_GSTIN) is None

    def test_clear_removes_entries(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))
        assert cache.clear() == 1
        assert cache.get(VALID_GSTIN) is None

    def test_a_second_cli_run_spends_no_captcha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The point of the whole thing: look twice, solve one captcha."""
        captchas = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captchas
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    captchas += 1
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=PAYLOAD)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        shared = DiskCache(directory=tmp_path)
        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            _client_factory(httpx.MockTransport(handler), cache=shared),
        )

        def pinned_cache(_args: argparse.Namespace) -> TaxpayerCache:
            return shared

        monkeypatch.setattr("gst_validator.cli._cache_for", pinned_cache)
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))

        assert main([VALID_GSTIN, "--json"]) == 0
        assert main([VALID_GSTIN, "--json"]) == 0
        capsys.readouterr()
        assert captchas == 1


class TestBatchInput:
    def test_several_gstins_emit_json_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--json"]) == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert [row["gstin"] for row in lines] == [VALID_GSTIN, PUBLIC_GSTIN]

    def test_a_single_gstin_still_prints_one_indented_object(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        stdout = capsys.readouterr().out
        assert stdout.startswith("{\n")  # unchanged from before batching
        assert json.loads(stdout)["gstin"] == VALID_GSTIN

    def test_stdin_is_read_for_a_dash(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{VALID_GSTIN}\n\n{PUBLIC_GSTIN}\n"))
        assert main(["-", "--offline", "--json"]) == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 2  # the blank line is skipped

    def test_worst_exit_code_wins(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "NOPE", "--offline"]) == 2
        captured = capsys.readouterr()
        assert "invalid GSTIN" in captured.err
        assert VALID_GSTIN in captured.out  # the valid one still reported

    def test_no_gstin_at_all_is_an_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--offline"]) == 2
        assert "no GSTIN given" in capsys.readouterr().err


class TestVersionFlag:
    def test_version_matches_the_distribution(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert metadata.version("gst-validator") in capsys.readouterr().out


class TestManufacturerPayload:
    """A second live capture: a manufacturer in another state.

    Caught that the Core Business Activity code for a manufacturer is "MFT",
    not the "MFR" its name suggests.
    """

    @pytest.fixture
    def manufacturer(self) -> TaxpayerDetails:
        path = Path(__file__).parent / "fixtures" / "taxpayer_manufacturer.json"
        return TaxpayerDetails.from_payload(json.loads(path.read_text()))

    def test_core_business_code(self, manufacturer: TaxpayerDetails) -> None:
        assert manufacturer.raw["ntcrbs"] == "MFT"
        assert manufacturer.core_business_activity == "Manufacturer"

    def test_nothing_is_dropped(self, manufacturer: TaxpayerDetails) -> None:
        assert manufacturer.unmapped == {}

    def test_shape_differences_from_the_service_fixture(
        self, manufacturer: TaxpayerDetails
    ) -> None:
        # This taxpayer has several business natures and no additional
        # addresses; the portal omits `adadr` entirely rather than sending [].
        assert len(manufacturer.nature_of_business) > 1
        assert manufacturer.additional_addresses == ()
        assert "adadr" not in manufacturer.raw
        assert manufacturer.principal_address is not None
        assert not manufacturer.principal_address.is_empty


class TestRegistrationType:
    """The 14th character is Z only for ordinary registrations."""

    # Same PAN and state, differing only in the 14th character, each with its
    # own correct check digit.
    REGULAR = "27ABCFE1234F1ZW"
    TDS = "27ABCFE1234F1D5"
    TCS = "27ABCFE1234F1C7"
    UNKNOWN = "27ABCFE1234F1QE"

    def test_tds_and_tcs_registrations_are_valid(self) -> None:
        for value, label in (
            (self.REGULAR, "Regular"),
            (self.TDS, "TDS deductor"),
            (self.TCS, "TCS collector"),
        ):
            gstin = GSTIN.parse(value)
            assert gstin.registration_type == label
            assert gstin.is_regular is (value == self.REGULAR)

    def test_checksum_still_guards_the_relaxed_position(self) -> None:
        with pytest.raises(InvalidGSTINError, match="checksum"):
            GSTIN(self.TDS[:-1] + "A")

    def test_unknown_type_character_is_not_labelled(self) -> None:
        gstin = GSTIN.parse(self.UNKNOWN)
        assert gstin.registration_type is None
        assert not gstin.is_regular

    def test_offline_json_reports_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([self.TDS, "--offline", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["registration_type"] == "TDS deductor"


class TestAsyncClientSurface:
    """The async client mirrors the sync one, so it needs the same cover."""

    @staticmethod
    def _run[T](coro: Callable[[], Coroutine[None, None, T]]) -> T:
        return asyncio.run(coro())

    def test_fetch_captcha(self) -> None:
        async def go() -> Captcha:
            async with AsyncGSTClient(transport=_transport(), cache=TTLCache()) as client:
                return await client.fetch_captcha()

        assert self._run(go).content == b"\x89PNG-bytes"

    def test_captcha_failure_is_wrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async def go() -> None:
            async with AsyncGSTClient(
                transport=httpx.MockTransport(handler), cache=TTLCache()
            ) as client:
                await client.fetch_captcha()

        with pytest.raises(CaptchaError):
            self._run(go)

    def test_cache_hit_skips_the_network(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("taxpayerDetails"):
                calls += 1
            return httpx.Response(200, json=PAYLOAD)

        cache = TTLCache()

        async def go() -> TaxpayerDetails | None:
            async with AsyncGSTClient(
                transport=httpx.MockTransport(handler), cache=cache
            ) as client:
                await client.fetch_details(VALID_GSTIN, "1a2b3")
                await client.fetch_details(VALID_GSTIN, "ignored")
                return client.cached(VALID_GSTIN)

        assert self._run(go) is not None
        assert calls == 1

    def test_lookup_failure_is_wrapped(self) -> None:
        async def go() -> None:
            async with AsyncGSTClient(transport=_transport(httpx.Response(503))) as client:
                await client.fetch_details(VALID_GSTIN, "1a2b3", refresh=True)

        with pytest.raises(TaxpayerLookupError):
            self._run(go)

    def test_captcha_free_endpoints(self) -> None:
        async def go() -> tuple[int, int, int]:
            async with AsyncGSTClient(transport=_transport(), cache=TTLCache()) as client:
                goods = await client.fetch_goods_and_services(VALID_GSTIN)
                years = await client.fetch_financial_years(VALID_GSTIN)
                prefs = await client.fetch_filing_preferences(VALID_GSTIN)
                return len(goods), len(years), len(prefs)

        assert self._run(go) == (1, 2, 2)


class TestTanBasedGstin:
    """A tax deductor may register with a TAN instead of a PAN.

    TAN is AAAA99999A where PAN is AAAAA9999A, so a pattern built only for PAN
    rejected every such registration outright.
    """

    TAN_BASED = "27MUMA12345B1D5"

    def test_accepted(self) -> None:
        gstin = GSTIN.parse(self.TAN_BASED)
        assert gstin.identifier_type == "TAN"
        assert gstin.tan == "MUMA12345B"
        assert gstin.pan is None
        assert gstin.registration_type == "TDS deductor"

    def test_entity_type_is_not_guessed_from_a_tan(self) -> None:
        # The 4th character of a TAN is the deductor's initial, not an entity
        # class, so reading it as one would invent a fact.
        assert GSTIN.parse(self.TAN_BASED).entity_type is None

    def test_pan_based_is_unaffected(self) -> None:
        gstin = GSTIN.parse(VALID_GSTIN)
        assert gstin.identifier_type == "PAN"
        assert gstin.pan == "ABCFE1234F"
        assert gstin.tan is None
        assert gstin.entity_type == "Firm / LLP"

    def test_checksum_still_applies(self) -> None:
        with pytest.raises(InvalidGSTINError, match="checksum"):
            GSTIN(self.TAN_BASED[:-1] + "A")

    def test_a_shape_that_is_neither_is_still_rejected(self) -> None:
        with pytest.raises(InvalidGSTINError, match="format"):
            GSTIN("27ABC123456X1ZW")

    def test_offline_json_distinguishes_them(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([self.TAN_BASED, "--offline", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["identifier_type"] == "TAN"
        assert payload["tan"] == "MUMA12345B"
        assert payload["pan"] is None


class TestRealWorldLayouts:
    """Published registrations of large companies, confirmed against the portal.

    Each of these was rejected by an earlier version of the pattern. They are
    public corporate registrations, printed on the invoices these companies
    issue, not anyone's personal data.
    """

    # Amazon Seller Services collects TCS under section 52: character 14 is C.
    AMAZON_TCS: ClassVar[list[str]] = ["27AAICA3918J1CT", "18AAICA3918J1CS", "03AAICA3918J2C2"]
    # The same company's ordinary registrations in other states.
    AMAZON_REGULAR: ClassVar[list[str]] = ["37AAICA3918J1ZH", "29AAICA3918J3ZC"]
    # Public-sector bodies that deduct TDS under section 51: character 14 is D.
    # NTPC, Indian Oil, and Indian Railways in three southern states.
    DEDUCTORS: ClassVar[list[str]] = [
        "07AAACN0255D1D9",
        "27AAACI1681G1DY",
        "32AAAGM0289C1D1",
        "33AAAGM0289C1DZ",
        "34AAAGM0289C1DX",
    ]
    # Indian Railways' ordinary registrations, published on its own site. The
    # 4th PAN character is G, the only Government example we have seen live.
    RAILWAYS: ClassVar[list[str]] = ["32AAAGM0289C1ZS", "33AAAGM0289C1ZQ", "34AAAGM0289C1ZO"]
    # Non-resident providers of online services, a different layout entirely.
    NON_RESIDENT: ClassVar[dict[str, tuple[str, int]]] = {
        "9917USA29016OS6": ("USA", 2017),
        "9923USA29044OSE": ("USA", 2023),
        "9924ISR29001OSH": ("ISR", 2024),
    }

    def test_tcs_collector_registrations(self) -> None:
        for value in self.AMAZON_TCS:
            gstin = GSTIN.parse(value)
            assert gstin.registration_type == "TCS collector"
            assert not gstin.is_regular
            assert gstin.pan == "AAICA3918J"

    def test_tds_deductor_registrations(self) -> None:
        for value in self.DEDUCTORS:
            gstin = GSTIN.parse(value)
            assert gstin.registration_type == "TDS deductor"
            assert not gstin.is_regular

    def test_same_company_mixes_regular_and_collector(self) -> None:
        """One PAN, several states, two kinds of registration."""
        pans = {GSTIN.parse(v).pan for v in self.AMAZON_TCS + self.AMAZON_REGULAR}
        assert pans == {"AAICA3918J"}
        assert {GSTIN.parse(v).registration_type for v in self.AMAZON_REGULAR} == {"Regular"}

    def test_government_entity_type(self) -> None:
        for value in self.RAILWAYS:
            gstin = GSTIN.parse(value)
            assert gstin.entity_type == "Government"
            assert gstin.pan == "AAAGM0289C"
            assert gstin.is_regular

    def test_one_body_holds_both_an_ordinary_and_a_deductor_registration(self) -> None:
        """Indian Railways, same PAN and state, differing only in character 14."""
        ordinary = GSTIN.parse("33AAAGM0289C1ZQ")
        deductor = GSTIN.parse("33AAAGM0289C1DZ")
        assert ordinary.pan == deductor.pan
        assert ordinary.state_name == deductor.state_name == "Tamil Nadu"
        assert ordinary.registration_type == "Regular"
        assert deductor.registration_type == "TDS deductor"

    def test_non_resident_layout(self) -> None:
        for value, (country, year) in self.NON_RESIDENT.items():
            gstin = GSTIN.parse(value)
            assert gstin.is_non_resident
            assert gstin.country_code == country
            assert gstin.registration_year == year
            # None of the PAN-based fields apply to this layout.
            assert gstin.pan is None
            assert gstin.tan is None
            assert gstin.identifier is None
            assert gstin.identifier_type is None
            assert gstin.state_name is None
            assert gstin.registration_sequence is None
            assert gstin.registration_type is None
            assert not gstin.is_regular

    def test_the_one_checksum_covers_every_layout(self) -> None:
        """The same mod-36 digit validates all of them, which is why they parse."""
        every = (
            self.AMAZON_TCS
            + self.AMAZON_REGULAR
            + self.DEDUCTORS
            + self.RAILWAYS
            + list(self.NON_RESIDENT)
        )
        for value in every:
            assert GSTIN.is_valid(value), value
            assert not GSTIN.is_valid(value[:-1] + ("A" if value[-1] != "A" else "B"))

    def test_offline_json_for_a_non_resident(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["9917USA29016OS6", "--offline", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["valid"] is True
        assert payload["pan"] is None


class TestStatutoryBodyPayload:
    """Central Warehousing Corporation: a statutory body in Tamil Nadu.

    Contributes shapes the other two fixtures lack: a constitution that is not
    a company, eight nature-of-business entries, e-invoicing switched on, and
    an aadhaar flag of "No" with no accompanying date field at all.
    """

    @pytest.fixture
    def body(self) -> TaxpayerDetails:
        path = Path(__file__).parent / "fixtures" / "taxpayer_statutory_body.json"
        return TaxpayerDetails.from_payload(json.loads(path.read_text()))

    def test_constitution_is_not_a_company(self, body: TaxpayerDetails) -> None:
        assert body.constitution == "Created under special act of parliament"

    def test_negative_flags_and_a_missing_date(self, body: TaxpayerDetails) -> None:
        assert body.aadhaar_verified is False
        assert body.aadhaar_verified_on is None  # "adhrVdt" is absent entirely
        assert "adhrVdt" not in body.raw
        assert body.einvoice_enabled is True  # the first live "Yes" we have seen
        assert body.is_field_visit_conducted is False

    def test_na_composition_rate_normalises(self, body: TaxpayerDetails) -> None:
        assert body.raw["cmpRt"] == "NA"
        assert body.composition_rate is None

    def test_many_business_natures(self, body: TaxpayerDetails) -> None:
        assert len(body.nature_of_business) == 8

    def test_nothing_is_dropped(self, body: TaxpayerDetails) -> None:
        assert body.unmapped == {}


class TestLegacyServiceCodes:
    """Taxpayers migrated in 2017 can still carry pre-GST accounting codes."""

    def test_eight_digit_service_tax_codes_parse(self) -> None:
        payload = {
            "bzsdtls": [
                {"saccd": "996511", "sdes": "Road transport services of Goods"},
                {"saccd": "00440193", "sdes": "STORAGE AND WAREHOUSE SERVICE"},
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=payload)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            items = client.fetch_goods_and_services(VALID_GSTIN)
        # Codes are kept verbatim: a SAC is not always six digits.
        assert [item.code for item in items] == ["996511", "00440193"]
        assert all(item.is_service for item in items)


class TestCancelledRegistration:
    """A registration the portal has cancelled.

    HDFC Ltd ceased to exist when it merged into HDFC Bank on 1 July 2023, and
    its registration carries that date. This is the only live payload with
    `cxdt` filled, so it is what `cancellation_date` and `is_cancelled` rest on.
    """

    @pytest.fixture
    def cancelled(self) -> TaxpayerDetails:
        path = Path(__file__).parent / "fixtures" / "taxpayer_cancelled.json"
        return TaxpayerDetails.from_payload(json.loads(path.read_text()))

    def test_status_is_more_than_the_word_cancelled(self, cancelled: TaxpayerDetails) -> None:
        """The portal says "Cancelled suo-moto", so an equality check would miss it."""
        assert cancelled.status == "Cancelled suo-moto"
        assert cancelled.is_cancelled
        assert not cancelled.is_active

    def test_cancellation_date_is_parsed(self, cancelled: TaxpayerDetails) -> None:
        assert cancelled.raw["cxdt"] == "01/07/2023"
        assert cancelled.cancellation_date == date(2023, 7, 1)

    def test_an_active_taxpayer_has_an_empty_cxdt(self) -> None:
        active = TaxpayerDetails.from_payload(
            json.loads((Path(__file__).parent / "fixtures" / "taxpayer_live.json").read_text())
        )
        assert active.raw["cxdt"] == ""
        assert active.cancellation_date is None

    def test_nothing_is_dropped(self, cancelled: TaxpayerDetails) -> None:
        assert cancelled.unmapped == {}


class TestOptionalPortalKeys:
    """Keys the portal omits for some taxpayers but not others.

    Observed across five live payloads: `ctb` is absent for one registration of
    a body that carries it in another state, and `adhrVdt` only appears when
    aadhaar is actually verified. Both must degrade to None, not raise.
    """

    def test_constitution_may_be_absent(self) -> None:
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN, "lgnm": "X"})
        assert details.constitution is None

    def test_aadhaar_date_absent_when_unverified(self) -> None:
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN, "adhrVFlag": "No"})
        assert details.aadhaar_verified is False
        assert details.aadhaar_verified_on is None


class TestUinLayout:
    """A Unique Identity Number, held by UN bodies and diplomatic missions.

    UNICEF India's 2317UNO00001UND: state 23, year 17, "UNO", serial 00001,
    "UN". Same shape as the non-resident layout but with a state code rather
    than "99", and the portal has ten years of filings for it.
    """

    UIN = "2317UNO00001UND"

    def test_parsed_and_distinguished_from_a_non_resident(self) -> None:
        gstin = GSTIN.parse(self.UIN)
        assert gstin.is_uin
        assert not gstin.is_non_resident
        assert gstin.holder_code == "UNO"
        assert gstin.registration_year == 2017
        assert gstin.state_name == "Madhya Pradesh"

    def test_pan_fields_do_not_apply(self) -> None:
        gstin = GSTIN.parse(self.UIN)
        for value in (gstin.pan, gstin.tan, gstin.identifier, gstin.identifier_type):
            assert value is None
        assert gstin.registration_type is None
        assert not gstin.is_regular

    def test_checksum_applies_to_this_layout_too(self) -> None:
        assert GSTIN.is_valid(self.UIN)
        assert not GSTIN.is_valid(self.UIN[:-1] + "A")

    def test_payload_is_a_un_body_with_several_keys_absent(self) -> None:
        path = Path(__file__).parent / "fixtures" / "taxpayer_uin.json"
        details = TaxpayerDetails.from_payload(json.loads(path.read_text()))
        assert details.taxpayer_type == "United Nation Body"
        assert details.is_active
        # A UIN payload carries no trade name, constitution, nature of
        # business or core business activity at all.
        for key in ("tradeNam", "ctb", "nba", "ntcrbs"):
            assert key not in details.raw
        assert details.trade_name is None
        assert details.constitution is None
        assert details.nature_of_business == ()
        assert details.core_business_activity is None
        assert details.unmapped == {}


class TestCompositionTaxpayer:
    """A composition dealer, from the portal's own composition list.

    The identity is replaced in the fixture because these registrations belong
    to individuals; every field that describes the registration is untouched.
    """

    @pytest.fixture
    def composition(self) -> TaxpayerDetails:
        path = Path(__file__).parent / "fixtures" / "taxpayer_composition.json"
        return TaxpayerDetails.from_payload(json.loads(path.read_text()))

    def test_scheme_shows_in_the_taxpayer_type(self, composition: TaxpayerDetails) -> None:
        """Not in the GSTIN: character 14 is "Z" here, as for any other dealer."""
        assert composition.taxpayer_type == "Composition"
        assert GSTIN.parse(composition.gstin).registration_type == "Regular"

    def test_proprietorship_constitution(self, composition: TaxpayerDetails) -> None:
        assert composition.constitution == "Proprietorship"
        assert GSTIN.parse(composition.gstin).entity_type == "Individual"

    def test_inactive_is_neither_active_nor_cancelled(self, composition: TaxpayerDetails) -> None:
        """A third status: the portal reports "Inactive", with a date in cxdt."""
        assert composition.status == "Inactive"
        assert not composition.is_active
        assert not composition.is_cancelled
        assert composition.cancellation_date is not None

    def test_composition_rate_is_still_not_sent(self, composition: TaxpayerDetails) -> None:
        """Even for a composition dealer the portal answers cmpRt "NA"."""
        assert composition.raw["cmpRt"] == "NA"
        assert composition.composition_rate is None

    def test_nothing_is_dropped(self, composition: TaxpayerDetails) -> None:
        assert composition.unmapped == {}
