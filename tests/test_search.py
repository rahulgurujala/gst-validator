"""The portal's other public searches: codes, lists, applications and documents."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from gst_validator import (
    ApplicationStatus,
    AsyncGSTClient,
    CompositionTaxpayer,
    GSTClient,
    GSTPractitioner,
    HSNCode,
    TaxpayerLookupError,
)
from gst_validator.cli import main

from .support import answer

FIXTURES = Path(__file__).parent / "fixtures"


def _transport(path_body: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    """Serve one body for whichever portal path the test calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/searchtp":
            return httpx.Response(200, text="<html></html>")
        if request.url.path == "/services/captcha":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        for fragment, body in path_body.items():
            if fragment in request.url.path:
                return httpx.Response(status, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestHSNCode:
    """Commodity and service codes. The one search that needs no captcha at all."""

    @pytest.fixture
    def rows(self) -> tuple[HSNCode, ...]:
        payload = json.loads((FIXTURES / "hsn_search.json").read_text())
        return tuple(HSNCode.from_payload(entry) for entry in payload["data"])

    def test_code_and_description(self, rows: tuple[HSNCode, ...]) -> None:
        assert rows[0].code == "3926"
        assert rows[0].description is not None
        assert rows[0].description.startswith("OTHER ARTICLES OF PLASTICS")

    def test_services_are_told_from_goods_by_their_prefix(self, rows: tuple[HSNCode, ...]) -> None:
        """The portal returns both from one endpoint with no flag to separate them."""
        assert [row.code for row in rows if row.is_service] == ["9983"]
        assert all(not row.is_service for row in rows if row.code.startswith("39"))

    def test_chapter(self, rows: tuple[HSNCode, ...]) -> None:
        assert rows[0].chapter == "39"
        assert HSNCode(code="").chapter is None

    def test_str_reads_as_code_then_description(self, rows: tuple[HSNCode, ...]) -> None:
        assert str(rows[2]) == "39261011 - OF POLYURETHANE FOAM"

    def test_search_returns_every_row(self) -> None:
        body = json.loads((FIXTURES / "hsn_search.json").read_text())
        with GSTClient(transport=_transport({"qsearch": body})) as client:
            found = client.search_hsn_codes("3926")
        assert len(found) == 4

    def test_an_unknown_code_is_empty_not_an_error(self) -> None:
        with GSTClient(transport=_transport({"qsearch": {"data": []}})) as client:
            assert client.search_hsn_codes("0000") == ()

    def test_description_search_sends_the_other_selector(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"data": []})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.search_hsn_codes("plastic", by="description")
        assert seen["selectedType"] == "byDesc"
        assert seen["inputText"] == "plastic"


class TestGSTPractitioner:
    """A public directory of named individuals, so it is handled carefully."""

    @pytest.fixture
    def rows(self) -> tuple[GSTPractitioner, ...]:
        payload = json.loads((FIXTURES / "practitioners.json").read_text())
        return tuple(GSTPractitioner.from_payload(entry) for entry in payload)

    def test_fields(self, rows: tuple[GSTPractitioner, ...]) -> None:
        assert rows[0].enrolment_number == "351800000001GP9"
        assert rows[0].category == "CAHCP"
        assert rows[0].pincode == "744103"  # lifted from the nested address
        assert rows[0].address == "1,EXAMPLE ROAD,EXAMPLE TOWN"

    def test_active_flag(self, rows: tuple[GSTPractitioner, ...]) -> None:
        assert [row.is_active for row in rows] == [True, False]

    def test_personal_contact_details_are_not_carried(
        self, rows: tuple[GSTPractitioner, ...]
    ) -> None:
        """A phone number and an email address in a CSV column make a scraper."""
        for row in rows:
            assert not hasattr(row, "contact_number")
            assert not hasattr(row, "email")

    def test_dropped_keys_are_not_reported_as_portal_changes(
        self, rows: tuple[GSTPractitioner, ...]
    ) -> None:
        """`unmapped` must mean "the portal grew a field", not "we declined one"."""
        assert all(row.unmapped == {} for row in rows)

    def test_a_genuinely_new_key_still_surfaces(self) -> None:
        row = GSTPractitioner.from_payload({"enrlNo": "X", "brandNew": "value"})
        assert row.unmapped == {"brandNew": "value"}

    def test_search_parses_a_bare_list(self) -> None:
        body = json.loads((FIXTURES / "practitioners.json").read_text())
        with GSTClient(transport=_transport({"gstp": body})) as client:
            found = client.search_practitioners(state_code="35")
        assert len(found) == 2

    def test_enrolment_number_switches_the_search_type(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=[])

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.search_practitioners(enrolment_number="351800000001GP9")
        assert seen["searchType"] == "E"
        assert seen["enrlNo"] == "351800000001GP9"


class TestCompositionTaxpayer:
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "gstin": "27ABCPE1234F1ZB",
        "lgnm": "A TRADER",
        "tradeNam": "Trader",
        "stcd": "27",
        "dtyp": "Composition",
        "rgdt": "01/04/2023",
    }

    def test_fields_and_number(self) -> None:
        row = CompositionTaxpayer.from_payload(dict(self.PAYLOAD))
        assert row.name == "Trader"
        assert row.registration_date == date(2023, 4, 1)
        assert row.number is not None
        assert row.number.state_name == "Maharashtra"

    def test_junk_gstin_degrades(self) -> None:
        assert CompositionTaxpayer.from_payload({"gstin": "NOPE"}).number is None

    def test_search_sends_the_scheme_direction(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"status": 1, "data": {"tpList": [self.PAYLOAD]}})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            rows = client.search_composition_taxpayers("27", "2025-2026", "1a2b3", opted_in=False)
        assert seen == {"op": "R", "captcha": "1a2b3", "stcd": "27", "fy": "2025-2026"}
        assert len(rows) == 1

    def test_empty_captcha_refused(self) -> None:
        with GSTClient(transport=_transport({})) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha"):
                client.search_composition_taxpayers("27", "2025-2026", "  ")


class TestApplicationStatus:
    def test_parsed(self) -> None:
        row = ApplicationStatus.from_payload(
            {
                "arn": "AA270125000000X",
                "status": "AP",
                "stsDesc": "Approved",
                "formNo": "REG-01",
                "submissionDate": "01/02/2025",
            }
        )
        assert row.status_description == "Approved"
        assert row.submitted_on == date(2025, 2, 1)
        assert "Approved" in str(row)

    def test_a_body_without_an_arn_is_a_rejection(self) -> None:
        body = {"errorCode": "SWEB_9000"}
        with GSTClient(transport=_transport({"trackarn": body})) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha"):
                client.track_application("AA270125000000X", "wrong")

    def test_tracked(self) -> None:
        body = {"arn": "AA270125000000X", "status": "AP", "stsDesc": "Approved"}
        with GSTClient(transport=_transport({"trackarn": body})) as client:
            assert client.track_application("aa270125000000x", "1a2b3").arn == "AA270125000000X"


class TestReferenceNumber:
    def test_a_genuine_document(self) -> None:
        body = {"valid": True, "docType": "Notice", "issueDate": "01/02/2025"}
        with GSTClient(transport=_transport({"verifyRfn": body})) as client:
            row = client.verify_reference_number("RF2701250000001", "1a2b3")
        assert row.is_genuine
        assert row.document_type == "Notice"
        assert "issued by the department" in str(row)

    def test_an_unrecognised_reference_is_an_answer_not_an_error(self) -> None:
        """Learning that a notice is fake is the point, so it must not raise."""
        with GSTClient(transport=_transport({"verifyRfn": {"valid": False}})) as client:
            row = client.verify_reference_number("RF0000000000000", "1a2b3")
        assert not row.is_genuine
        assert "not recognised" in str(row)

    def test_a_portal_rejection_still_raises(self) -> None:
        body = {"errorCode": "SWEB_9000"}
        with GSTClient(transport=_transport({"verifyRfn": body})) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha"):
                client.verify_reference_number("RF2701250000001", "wrong")


class TestTemporaryRegistration:
    def test_parsed(self) -> None:
        body = {"tempId": "271700000000TMP", "lgnm": "SOMEONE", "status": "Active"}
        with GSTClient(transport=_transport({"smreg": body})) as client:
            row = client.search_temporary_registration("271700000000tmp", "1a2b3")
        assert row.temporary_id == "271700000000TMP"
        assert row.legal_name == "SOMEONE"

    def test_missing_id_is_a_rejection(self) -> None:
        with GSTClient(transport=_transport({"smreg": {"errorCode": "SWEB_9000"}})) as client:
            with pytest.raises(TaxpayerLookupError):
                client.search_temporary_registration("271700000000TMP", "wrong")


class TestAsyncParity:
    """Every new search exists on the async client and returns the same thing."""

    def test_hsn_and_practitioners(self) -> None:
        import asyncio

        hsn = json.loads((FIXTURES / "hsn_search.json").read_text())
        gstp = json.loads((FIXTURES / "practitioners.json").read_text())

        async def go() -> tuple[int, int]:
            async with AsyncGSTClient(transport=_transport({"qsearch": hsn, "gstp": gstp})) as c:
                codes = await c.search_hsn_codes("3926")
                people = await c.search_practitioners(state_code="35")
                return len(codes), len(people)

        assert asyncio.run(go()) == (4, 2)

    def test_every_sync_search_has_an_async_twin(self) -> None:
        import inspect

        for name in (
            "search_hsn_codes",
            "search_practitioners",
            "search_composition_taxpayers",
            "track_application",
            "verify_reference_number",
            "search_temporary_registration",
        ):
            assert hasattr(GSTClient, name), name
            assert inspect.iscoroutinefunction(getattr(AsyncGSTClient, name)), name


class TestSearchCLI:
    """Each search is its own CLI mode, with the same five output formats."""

    @staticmethod
    def _factory(bodies: dict[str, Any]) -> Callable[..., GSTClient]:
        def build(**_: object) -> GSTClient:
            return GSTClient(transport=_transport(bodies))

        return build

    def test_hsn_json(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = json.loads((FIXTURES / "hsn_search.json").read_text())
        monkeypatch.setattr("gst_validator.cli.GSTClient", self._factory({"qsearch": body}))
        assert main(["--hsn", "3926", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 4
        assert rows[0]["code"] == "3926"
        assert "unmapped" not in rows[0]  # reported as `extra`, and only when present

    def test_hsn_csv_has_no_empty_extra_column(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = json.loads((FIXTURES / "hsn_search.json").read_text())
        monkeypatch.setattr("gst_validator.cli.GSTClient", self._factory({"qsearch": body}))
        assert main(["--hsn", "3926", "--format", "csv"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "code,description"

    def test_hsn_takes_several_terms_from_stdin(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No captcha, so a batch costs nothing."""
        import io

        body = json.loads((FIXTURES / "hsn_search.json").read_text())
        monkeypatch.setattr("gst_validator.cli.GSTClient", self._factory({"qsearch": body}))
        monkeypatch.setattr("sys.stdin", io.StringIO("3926\n9983\n"))
        assert main(["--hsn", "-", "--format", "jsonl"]) == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 8  # 4 rows per term

    def test_practitioner_needs_narrowing(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A bare directory dump is not what this is for."""
        assert main(["--practitioner"]) == 2
        assert "--state, --pincode or --enrolment" in capsys.readouterr().err

    def test_practitioner_single_lookup(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = json.loads((FIXTURES / "practitioners.json").read_text())
        monkeypatch.setattr("gst_validator.cli.GSTClient", self._factory({"gstp": body}))
        assert main(["--practitioner", "--enrolment", "351800000001GP9", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["enrolment_number"] == "351800000001GP9"
        assert "contact" not in json.dumps(rows).lower()

    def test_composition_needs_state_and_year(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--composition", "--state", "27"]) == 2
        assert "needs both --state and --year" in capsys.readouterr().err

    def test_arn_single_answer(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = {"arn": "AA270125000000X", "status": "AP", "stsDesc": "Approved"}
        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            self._factory({"trackarn": body}),
        )
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main(["--arn", "AA270125000000X", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status_description"] == "Approved"

    def test_rfn_reports_a_fake_without_failing(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "gst_validator.cli.GSTClient", self._factory({"verifyRfn": {"valid": False}})
        )
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main(["--rfn", "RF0000000000000", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["is_genuine"] is False
