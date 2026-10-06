"""Parsing what the portal returns about a taxpayer."""

import json
from datetime import date
from pathlib import Path

import pytest

from gst_validator import (
    GSTIN,
    Address,
    Captcha,
    TaxpayerDetails,
)

from .support import (
    PAYLOAD,
    VALID_GSTIN,
)


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


class TestCaptcha:
    def test_data_uri(self) -> None:
        assert Captcha(b"ab").data_uri == "data:image/png;base64,YWI="
