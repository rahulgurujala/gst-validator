"""Offline tests: every HTTP call goes through httpx.MockTransport."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from importlib import metadata
from pathlib import Path

import httpx
import pytest

import gst_validator
from gst_validator import (
    GSTIN,
    Address,
    AsyncGSTClient,
    Captcha,
    CaptchaError,
    GSTClient,
    InvalidGSTINError,
    NullCache,
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
def isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFAULT_CACHE is process-wide by design; give each test a fresh one."""
    monkeypatch.setattr("gst_validator.client.DEFAULT_CACHE", TTLCache())


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
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target)]) == 0
        assert not target.exists()
        assert "ACME TRADERS" in capsys.readouterr().out

    def test_keep_captcha_retains_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "captcha.png"
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target), "--keep-captcha"]) == 0
        assert target.read_bytes() == b"\x89PNG-bytes"


def _answer(text: str) -> Callable[..., str]:
    """Stand-in for ``input`` that always returns ``text``."""

    def prompt(*_args: object, **_kwargs: object) -> str:
        return text

    return prompt


class _FakeClient(GSTClient):
    """GSTClient pinned to a mock transport, usable where the CLI builds one."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__(transport=transport, cache=TTLCache())


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
            "pan": "ABCFE1234F",
            "entity_type": "Firm / LLP",
            "registration_sequence": "1",
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
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--json"]) == 0
        stdout = capsys.readouterr().out
        assert "\x1b[" not in stdout  # no ANSI escapes
        assert json.loads(stdout)["legal_name"] == "ACME TRADERS"

    def test_raw_output_round_trips(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--raw"]) == 0
        assert json.loads(capsys.readouterr().out) == PAYLOAD

    def test_table_renders_objects_not_dicts(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
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

        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
        monkeypatch.setattr("builtins.input", prompting_input)
        assert main([VALID_GSTIN, "--json"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN
        assert "captcha text" in captured.err

    def test_captcha_data_uri_goes_to_stderr(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--captcha-base64 with --json must leave stdout as pure JSON."""
        monkeypatch.setattr("gst_validator.cli.GSTClient", lambda: _FakeClient(_transport()))
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
            "gst_validator.cli.GSTClient", lambda: _FakeClient(httpx.MockTransport(handler))
        )
        monkeypatch.setattr("builtins.input", _answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "[bold red]INJECTED[/]" in stdout  # printed, not interpreted
