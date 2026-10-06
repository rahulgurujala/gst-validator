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

    @staticmethod
    def _params_seen(by: str = "code", *, goods: bool = True) -> dict[str, str]:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"data": []})

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.search_hsn_codes("plastic", by=by, goods=goods)
        return seen

    def test_a_code_search_needs_no_category(self) -> None:
        seen = self._params_seen()
        assert seen["selectedType"] == "byCode"
        assert seen["category"] == "null"

    def test_a_description_search_must_send_a_category(self) -> None:
        """Without one the portal answers with an empty list and no error, so
        shipping it unset looked like "no matches" rather than a broken call."""
        assert self._params_seen("description")["category"] == "P"
        assert self._params_seen("description", goods=False)["category"] == "S"

    def test_description_search_selects_the_right_endpoint_mode(self) -> None:
        seen = self._params_seen("description")
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

    @staticmethod
    def _body_seen(
        *, enrolment_number: str | None = None, state_code: str | None = None
    ) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=[])

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            client.search_practitioners(enrolment_number=enrolment_number, state_code=state_code)
        return seen

    def test_an_enrolment_search_is_type_b(self) -> None:
        """ "E" looked like the obvious letter and is refused with FO8001."""
        seen = self._body_seen(enrolment_number="351800000001gp9")
        assert seen["searchType"] == "B"
        assert seen["enrlNo"] == "351800000001GP9"

    def test_an_area_search_is_type_a(self) -> None:
        seen = self._body_seen(state_code="35")
        assert seen["searchType"] == "A"
        assert seen["stCd"] == "35"

    def test_an_area_search_carries_no_enrolment_key(self) -> None:
        """The two shapes are not interchangeable: adding enrlNo here earns
        SWEB_8000, which is how this was broken once already."""
        assert "enrlNo" not in self._body_seen(state_code="35")

    def test_unused_fields_are_empty_strings_not_nulls(self) -> None:
        """The portal refuses nulls here, which is how FO8001 was reached."""
        seen = self._body_seen(enrolment_number="351800000001GP9")
        assert seen["trpNam"] == ""
        assert seen["stCd"] == ""
        assert seen["pinCd"] == ""


class TestCompositionTaxpayer:
    """Pinned to a captured response: the row carries gstin, lnm, indt, oudt.

    Six of the seven field names this was first written against did not exist.
    The fixture keeps the real shape with invented identities, because these
    registrations belong to sole proprietors rather than companies.
    """

    PAYLOAD: ClassVar[dict[str, Any]] = {
        "gstin": "27ABCPE1234F1ZB",
        "lnm": "A SOLE TRADER",
        "oudt": "31/03/2025",
        "indt": "01/04/2024",
    }

    def test_the_four_fields_the_portal_actually_sends(self) -> None:
        row = CompositionTaxpayer.from_payload(dict(self.PAYLOAD))
        assert row.legal_name == "A SOLE TRADER"
        assert row.opted_in_on == date(2024, 4, 1)
        assert row.opted_out_on == date(2025, 3, 31)
        assert row.unmapped == {}

    def test_the_state_comes_from_the_number(self) -> None:
        """The row carries no state code, so it is decoded rather than read."""
        row = CompositionTaxpayer.from_payload(dict(self.PAYLOAD))
        assert row.state_code == "27"
        assert row.number is not None
        assert row.number.state_name == "Maharashtra"

    def test_the_captured_fixture_parses_whole(self) -> None:
        rows = [
            CompositionTaxpayer.from_payload(entry)
            for entry in json.loads((FIXTURES / "composition_list.json").read_text())
        ]
        assert len(rows) == 2
        assert all(row.number for row in rows)
        assert all(row.unmapped == {} for row in rows)

    def test_junk_gstin_degrades(self) -> None:
        row = CompositionTaxpayer.from_payload({"gstin": "NOPE"})
        assert row.number is None
        assert row.state_code is None

    def test_search_sends_the_scheme_direction(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=[self.PAYLOAD])

        with GSTClient(transport=httpx.MockTransport(handler)) as client:
            rows = client.search_composition_taxpayers("27", "2025-2026", "1a2b3", opted_in=False)
        assert seen == {"op": "R", "captcha": "1a2b3", "stcd": "27", "fy": "2025-2026"}
        assert len(rows) == 1

    def test_a_success_is_a_bare_list_not_an_envelope(self) -> None:
        """This endpoint does not use the {"status": 1, "data": ...} wrapper the
        other list endpoints do. Parsing it as one raised "unexpected payload
        type list" on every real call, including successful ones."""
        row = {"gstin": "27ABCPE1234F1ZB", "lgnm": "A TRADER"}
        with GSTClient(transport=_transport({"opteddata": [row]})) as client:
            rows = client.search_composition_taxpayers("27", "2025-2026", "1a2b3")
        assert len(rows) == 1
        assert rows[0].gstin == "27ABCPE1234F1ZB"

    def test_an_empty_list_means_no_match_not_a_failure(self) -> None:
        """Observed live: Maharashtra 2025-2026 opted-in answers []."""
        with GSTClient(transport=_transport({"opteddata": []})) as client:
            assert client.search_composition_taxpayers("27", "2025-2026", "1a2b3") == ()

    def test_a_rejection_is_an_object_and_still_raises(self) -> None:
        """Success and failure are told apart by the type of the body."""
        with GSTClient(transport=_transport({"opteddata": {"errorCode": "SWEB_9000"}})) as client:
            with pytest.raises(TaxpayerLookupError, match="captcha"):
                client.search_composition_taxpayers("27", "2025-2026", "wrong")

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
        assert "--enrolment" in capsys.readouterr().err

    def test_a_pincode_alone_is_refused_before_the_network(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The portal refuses it with EM_SRS_FO_016_02, so do not spend a call."""
        assert main(["--practitioner", "--pincode", "744103"]) == 2
        assert "--state" in capsys.readouterr().err

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
