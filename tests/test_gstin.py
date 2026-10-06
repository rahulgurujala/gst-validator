"""The GSTIN itself: layouts, checksums and what each one encodes."""

import json
from pathlib import Path
from typing import ClassVar

import pytest

from gst_validator import (
    GSTIN,
    InvalidGSTINError,
    TaxpayerDetails,
)
from gst_validator.cli import main

from .support import (
    VALID_GSTIN,
)


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


class TestGSTINExtras:
    def test_decodes_state_and_entity(self) -> None:
        gstin = GSTIN(VALID_GSTIN)
        assert gstin.state_name == "Maharashtra"
        assert gstin.entity_type == "Firm / LLP"
        assert gstin.registration_sequence == "1"

    def test_is_valid_does_not_raise(self) -> None:
        assert GSTIN.is_valid(VALID_GSTIN)
        assert not GSTIN.is_valid("NOPE")


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
            assert gstin.holder_code == country
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


class TestStateCodes:
    """Checked against the official master codes published on the NIC
    e-invoice portal, https://einvoice1.gst.gov.in/Others/MasterCodes
    """

    @staticmethod
    def _for_state(code: str) -> GSTIN:
        """A checksum-valid GSTIN in the given state, built on a dummy PAN."""
        prefix = f"{code}ABCFE1234F1Z"
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        total = 0
        for position, character in enumerate(prefix):
            product = alphabet.index(character) * (1 + position % 2)
            total += product // 36 + product % 36
        return GSTIN(prefix + alphabet[(36 - total % 36) % 36])

    def test_every_state_code_in_use_is_named(self) -> None:
        """01-38 are the states and union territories currently issued."""
        unnamed = [
            f"{n:02d}" for n in range(1, 39) if self._for_state(f"{n:02d}").state_name is None
        ]
        assert unnamed == []

    def test_codes_outside_the_state_range(self) -> None:
        assert self._for_state("96").state_name == "Other Countries"
        assert self._for_state("97").state_name == "Other Territory"

    def test_retired_code_is_kept_for_older_registrations(self) -> None:
        """28 was Andhra Pradesh before the Telangana split."""
        name = self._for_state("28").state_name
        assert name is not None
        assert "retired" in name

    def test_an_unassigned_code_is_not_invented(self) -> None:
        assert self._for_state("50").state_name is None

    def test_codes_seen_on_real_registrations(self) -> None:
        for value, expected in (
            ("27AAACR5055K1Z7", "Maharashtra"),
            ("29AAACI4798L1ZU", "Karnataka"),
            ("19AAACI5950L1Z7", "West Bengal"),
            ("33AAACC1206D1ZN", "Tamil Nadu"),
            ("32AAAGM0289C1ZS", "Kerala"),
            ("34AAAGM0289C1ZO", "Puducherry"),
            ("18AAICA3918J1CS", "Assam"),
            ("03AAICA3918J2C2", "Punjab"),
            ("2317UNO00001UND", "Madhya Pradesh"),
        ):
            assert GSTIN.parse(value).state_name == expected, value
