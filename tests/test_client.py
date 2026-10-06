"""The HTTP clients, against mock transports only."""

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path

import httpx
import pytest

from gst_validator import (
    AsyncGSTClient,
    Captcha,
    CaptchaError,
    GSTClient,
    InvalidGSTINError,
    InvalidPANError,
    TaxpayerDetails,
    TaxpayerLookupError,
    TaxpayerProfile,
    TTLCache,
)

from .support import (
    GOODS_PAYLOAD,
    PAN,
    PAYLOAD,
    REGISTRATIONS_PAYLOAD,
    VALID_GSTIN,
    transport,
)


class TestGSTClient:
    def test_fetch_captcha(self) -> None:
        with GSTClient(transport=transport()) as client:
            captcha = client.fetch_captcha()
        assert captcha.content == b"\x89PNG-bytes"
        assert captcha.data_uri.startswith("data:image/png;base64,")

    def test_fetch_details(self) -> None:
        with GSTClient(transport=transport()) as client:
            details = client.fetch_details(VALID_GSTIN, " 1a2b3 ")
        assert details.legal_name == "ACME TRADERS"
        assert details.constitution == "Partnership"
        assert details.is_active
        assert not details.is_cancelled

    def test_empty_captcha_text_rejected(self) -> None:
        with GSTClient(transport=transport()) as client, pytest.raises(TaxpayerLookupError):
            client.fetch_details(VALID_GSTIN, "   ")

    def test_portal_error_message_raised(self) -> None:
        error = httpx.Response(200, json={"errorMsg": "Invalid captcha"})
        with (
            GSTClient(transport=transport(error)) as client,
            pytest.raises(TaxpayerLookupError, match="Invalid captcha"),
        ):
            client.fetch_details(VALID_GSTIN, "wrong")

    def test_error_code_without_message_explained(self) -> None:
        """The portal rejects with HTTP 200, a null message and only a code."""
        rejected = httpx.Response(200, json={"url": "/", "message": None, "errorCode": "SWEB_9000"})
        with GSTClient(transport=transport(rejected)) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha") as caught:
                client.fetch_details(VALID_GSTIN, "702603")
        assert caught.value.code == "SWEB_9000"

    def test_unknown_error_code_still_raises(self) -> None:
        rejected = httpx.Response(200, json={"errorCode": "SWEB_1234"})
        with GSTClient(transport=transport(rejected)) as client:
            with pytest.raises(TaxpayerLookupError, match="no taxpayer details"):
                client.fetch_details(VALID_GSTIN, "702603")

    def test_http_error_wrapped(self) -> None:
        with (
            GSTClient(transport=transport(httpx.Response(503))) as client,
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
        with GSTClient(transport=transport()) as client:
            items = client.fetch_goods_and_services(VALID_GSTIN)
        assert len(items) == 1
        assert items[0].code == "998314"
        assert items[0].is_service
        assert str(items[0]).startswith("998314 - Information")

    def test_financial_years(self) -> None:
        with GSTClient(transport=transport()) as client:
            years = client.fetch_financial_years(VALID_GSTIN)
        assert [year.label for year in years] == ["2025-2026", "2026-2027"]
        assert years[0].start_year == 2025

    def test_filing_preferences(self) -> None:
        with GSTClient(transport=transport()) as client:
            preferences = client.fetch_filing_preferences(VALID_GSTIN)
        assert preferences[0].is_quarterly
        assert preferences[1].is_monthly
        assert str(preferences[0]) == "Q1: quarterly"

    def test_profile_bundles_everything(self) -> None:
        with GSTClient(transport=transport()) as client:
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
            async with AsyncGSTClient(transport=transport(), cache=TTLCache()) as client:
                return await client.fetch_profile(VALID_GSTIN, "1a2b3")

        profile = asyncio.run(run())
        assert profile.gstin == VALID_GSTIN
        assert profile.details.legal_name == "ACME TRADERS"
        assert len(profile.goods_and_services) == 1
        assert len(profile.filing_preferences) == 2


class TestAsyncClientSurface:
    """The async client mirrors the sync one, so it needs the same cover."""

    @staticmethod
    def _run[T](coro: Callable[[], Coroutine[None, None, T]]) -> T:
        return asyncio.run(coro())

    def test_fetch_captcha(self) -> None:
        async def go() -> Captcha:
            async with AsyncGSTClient(transport=transport(), cache=TTLCache()) as client:
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
            async with AsyncGSTClient(transport=transport(httpx.Response(503))) as client:
                await client.fetch_details(VALID_GSTIN, "1a2b3", refresh=True)

        with pytest.raises(TaxpayerLookupError):
            self._run(go)

    def test_captcha_free_endpoints(self) -> None:
        async def go() -> tuple[int, int, int]:
            async with AsyncGSTClient(transport=transport(), cache=TTLCache()) as client:
                goods = await client.fetch_goods_and_services(VALID_GSTIN)
                years = await client.fetch_financial_years(VALID_GSTIN)
                prefs = await client.fetch_filing_preferences(VALID_GSTIN)
                return len(goods), len(years), len(prefs)

        assert self._run(go) == (1, 2, 2)


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


class TestPanSearch:
    """Every GSTIN held under one PAN. Costs a captcha, like the detail lookup."""

    def test_returns_each_registration(self) -> None:
        with GSTClient(transport=transport()) as client:
            rows = client.fetch_registrations_by_pan(PAN, "1a2b3")
        assert len(rows) == 7
        assert rows[0].gstin == "24AAACR5055K2ZC"
        assert rows[0].status == "Inactive"
        assert {row.number.pan for row in rows if row.number} == {"AAACR5055K"}

    def test_the_pan_is_normalised_before_it_is_sent(self) -> None:
        sent: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("gstndtls"):
                sent.update(json.loads(request.content))
                return httpx.Response(200, json=REGISTRATIONS_PAYLOAD)
            return httpx.Response(200, text="<html></html>")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.fetch_registrations_by_pan("  aaacr5055k ", "  1a2b3  ")
        assert sent == {"panNO": "AAACR5055K", "captcha": "1a2b3"}

    def test_a_bad_pan_never_reaches_the_network(self) -> None:
        """A captcha is a person's time: do not spend one on a typo."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be called")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(InvalidPANError):
                client.fetch_registrations_by_pan("NOPE", "1a2b3")

    def test_empty_captcha_text_rejected(self) -> None:
        with GSTClient(transport=transport()) as client, pytest.raises(TaxpayerLookupError):
            client.fetch_registrations_by_pan(PAN, "   ")

    def test_a_rejection_carries_no_list_and_raises(self) -> None:
        """The live rejection: HTTP 200, SWEB_9000, no gstinResList at all."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("gstndtls"):
                return httpx.Response(
                    200, json={"url": "/", "message": None, "errorCode": "SWEB_9000"}
                )
            return httpx.Response(200, text="<html></html>")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha") as caught:
                client.fetch_registrations_by_pan(PAN, "wrong")
        assert caught.value.code == "SWEB_9000"

    def test_a_pan_with_no_registrations_is_empty_not_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("gstndtls"):
                return httpx.Response(200, json={"gstinResList": []})
            return httpx.Response(200, text="<html></html>")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            assert client.fetch_registrations_by_pan(PAN, "1a2b3") == ()

    def test_http_failure_is_wrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TaxpayerLookupError, match="request to /api/get/gstndtls failed"):
                client.fetch_registrations_by_pan(PAN, "1a2b3")

    def test_the_request_is_refered_from_the_pan_search_page(self) -> None:
        """The portal fingerprints clients; this endpoint has its own referer."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("gstndtls"):
                seen.update(request.headers)
                return httpx.Response(200, json=REGISTRATIONS_PAYLOAD)
            return httpx.Response(200, text="<html></html>")

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.fetch_registrations_by_pan(PAN, "1a2b3")
        assert seen["referer"].endswith("/searchtpbypan")

    def test_the_async_client_matches(self) -> None:
        async def go() -> tuple[int, str]:
            async with AsyncGSTClient(transport=transport(), cache=TTLCache()) as client:
                rows = await client.fetch_registrations_by_pan(PAN, "1a2b3")
                return len(rows), rows[1].gstin

        assert asyncio.run(go()) == (7, "14AAACR5055K1ZE")

    def test_the_async_client_also_validates_before_the_network(self) -> None:
        async def go() -> None:
            async with AsyncGSTClient(transport=transport(), cache=TTLCache()) as client:
                await client.fetch_registrations_by_pan("NOPE", "1a2b3")

        with pytest.raises(InvalidPANError):
            asyncio.run(go())
