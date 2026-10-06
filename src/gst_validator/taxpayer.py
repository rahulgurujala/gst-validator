"""What the portal returns about a taxpayer."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Self

from ._parsing import as_date, as_flag, as_mapping, as_sequence, as_text
from .exceptions import InvalidGSTINError
from .gstin import GSTIN

__all__ = [
    "Address",
    "FilingPreference",
    "FinancialYear",
    "GoodsOrService",
    "Jurisdiction",
    "TaxpayerDetails",
    "TaxpayerProfile",
]


# ``ntcrbs`` is the taxpayer's Core Business Activity, a field the portal
# added in March 2021 with exactly three choices: Manufacturer, Trader, and
# Service Provider and Others (wholesaler and retailer are sub-types of
# Trader, not categories of their own). Both codes below were read off live
# responses: "MFT" is the manufacturer code, not the "MFR" the name suggests,
# which is why the trader code is left out until one is actually observed.
# An unrecognised code passes through unchanged rather than being guessed at.
_CORE_BUSINESS: Final[dict[str, str]] = {
    "SPO": "Service Provider and Others",
    "MFT": "Manufacturer",
}


_MAPPED_KEYS: Final = frozenset(
    {
        "gstin",
        "lgnm",
        "tradeNam",
        "sts",
        "ctb",
        "dty",
        "rgdt",
        "cxdt",
        "lstupdt",
        "nba",
        "pradr",
        "adadr",
        "ctj",
        "ctjCd",
        "stj",
        "stjCd",
        "einvoiceStatus",
        "isFieldVisitConducted",
        "ntcrbs",
        "adhrVFlag",
        "adhrVdt",
        "ekycVFlag",
        "cmpRt",
    }
)


@dataclass(frozen=True, slots=True)
class Address:
    """A registered place of business."""

    floor: str | None = None
    building_number: str | None = None
    building_name: str | None = None
    street: str | None = None
    location: str | None = None
    landmark: str | None = None
    city: str | None = None
    district: str | None = None
    state: str | None = None
    pincode: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    nature_of_business: tuple[str, ...] = ()
    full: str | None = None
    """The portal's own one-line rendering (``adr``), when it sends one."""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``pradr``/``adadr`` entry.

        The portal is inconsistent here: the split fields may sit under
        ``addr``, directly on the entry, or be replaced by a single ``adr``
        string, so all three shapes are accepted.
        """
        addr = as_mapping(payload.get("addr")) or payload
        nature = as_text(payload.get("ntr")) or as_text(addr.get("ntr"))
        return cls(
            floor=as_text(addr.get("flno")),
            building_number=as_text(addr.get("bno")),
            building_name=as_text(addr.get("bnm")),
            street=as_text(addr.get("st")),
            location=as_text(addr.get("loc")),
            landmark=as_text(addr.get("landMark")),
            city=as_text(addr.get("city")),
            district=as_text(addr.get("dst")),
            state=as_text(addr.get("stcd")),
            pincode=as_text(addr.get("pncd")),
            latitude=as_text(addr.get("lt")),
            longitude=as_text(addr.get("lg")),
            nature_of_business=tuple(part.strip() for part in nature.split(",")) if nature else (),
            full=as_text(addr.get("adr")) or as_text(payload.get("adr")),
        )

    @property
    def is_empty(self) -> bool:
        return not self.as_line()

    def as_line(self) -> str:
        """Single-line postal rendering, empty parts dropped."""
        parts = (
            self.floor,
            self.building_number,
            self.building_name,
            self.street,
            self.location,
            self.landmark,
            self.city,
            self.district,
            self.state,
            self.pincode,
        )
        joined = ", ".join(part for part in parts if part)
        return joined or (self.full or "")

    def __str__(self) -> str:
        return self.as_line()


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    """Central or state tax jurisdiction the taxpayer falls under."""

    office: str | None = None
    code: str | None = None

    @property
    def is_known(self) -> bool:
        return self.office is not None or self.code is not None

    def __str__(self) -> str:
        return " ".join(part for part in (self.office, self.code and f"({self.code})") if part)


@dataclass(frozen=True, slots=True)
class TaxpayerDetails:
    """Structured view of the portal's ``taxpayerDetails`` payload.

    Every field is optional because the portal omits keys per taxpayer type;
    :attr:`raw` always holds the untouched body for anything not modelled here.
    """

    gstin: str
    legal_name: str | None = None
    trade_name: str | None = None
    status: str | None = None
    """``sts``. Seen live as "Active", "Inactive" and "Cancelled suo-moto"."""
    constitution: str | None = None
    taxpayer_type: str | None = None
    registration_date: date | None = None
    cancellation_date: date | None = None
    last_updated: date | None = None
    """``lstupdt``, which the public search endpoint has never been seen to send."""
    nature_of_business: tuple[str, ...] = ()
    principal_address: Address | None = None
    additional_addresses: tuple[Address, ...] = ()
    """Extra places of business.

    The public search endpoint has not been seen to return ``adadr`` in any
    live lookup, across companies, a bank, a manufacturer and two statutory
    registrations, so this may only ever be populated from another source.
    """
    central_jurisdiction: Jurisdiction = field(default_factory=Jurisdiction)
    state_jurisdiction: Jurisdiction = field(default_factory=Jurisdiction)
    einvoice_enabled: bool | None = None
    is_field_visit_conducted: bool | None = None
    core_business_activity: str | None = None
    aadhaar_verified: bool | None = None
    aadhaar_verified_on: date | None = None
    ekyc_status: str | None = None
    composition_rate: str | None = None
    """``cmpRt``, which the portal answers as "NA" even for a composition dealer."""
    raw: dict[str, Any] = field(repr=False, default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from the portal's ``taxpayerDetails`` JSON body."""
        nature = as_sequence(payload.get("nba"))
        additional = as_sequence(payload.get("adadr"))
        principal = as_mapping(payload.get("pradr"))
        return cls(
            gstin=as_text(payload.get("gstin")) or "",
            legal_name=as_text(payload.get("lgnm")),
            trade_name=as_text(payload.get("tradeNam")),
            status=as_text(payload.get("sts")),
            constitution=as_text(payload.get("ctb")),
            taxpayer_type=as_text(payload.get("dty")),
            registration_date=as_date(payload.get("rgdt")),
            cancellation_date=as_date(payload.get("cxdt")),
            last_updated=as_date(payload.get("lstupdt")),
            nature_of_business=tuple(str(item) for item in nature),
            principal_address=Address.from_payload(principal) if principal else None,
            additional_addresses=tuple(
                Address.from_payload(mapped)
                for entry in additional
                if (mapped := as_mapping(entry))
            ),
            central_jurisdiction=Jurisdiction(
                office=as_text(payload.get("ctj")), code=as_text(payload.get("ctjCd"))
            ),
            state_jurisdiction=Jurisdiction(
                office=as_text(payload.get("stj")), code=as_text(payload.get("stjCd"))
            ),
            einvoice_enabled=as_flag(payload.get("einvoiceStatus")),
            is_field_visit_conducted=as_flag(payload.get("isFieldVisitConducted")),
            core_business_activity=_CORE_BUSINESS.get(
                as_text(payload.get("ntcrbs")) or "", as_text(payload.get("ntcrbs"))
            ),
            aadhaar_verified=as_flag(payload.get("adhrVFlag")),
            aadhaar_verified_on=as_date(payload.get("adhrVdt")),
            ekyc_status=as_text(payload.get("ekycVFlag")),
            composition_rate=as_text(payload.get("cmpRt")),
            raw=payload,
        )

    @property
    def number(self) -> GSTIN | None:
        """The GSTIN as a value object, or ``None`` if the portal echoed junk."""
        try:
            return GSTIN.parse(self.gstin)
        except InvalidGSTINError:
            return None

    @property
    def name(self) -> str | None:
        """Trade name when present, else the legal name."""
        return self.trade_name or self.legal_name

    @property
    def is_active(self) -> bool:
        """True only for status "Active"; "Inactive" is not active either."""
        return (self.status or "").casefold() == "active"

    @property
    def is_cancelled(self) -> bool:
        """True for any cancelled status.

        Matched on the prefix because the portal says "Cancelled suo-moto"
        rather than the bare word, which an equality check would miss.
        """
        return (self.status or "").casefold().startswith("cancel")

    @property
    def addresses(self) -> tuple[Address, ...]:
        """Principal address first, then any additional places of business."""
        head = (self.principal_address,) if self.principal_address else ()
        return head + self.additional_addresses

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready dict of the modelled fields (dates as ISO strings)."""
        return {
            "gstin": self.gstin,
            "legal_name": self.legal_name,
            "trade_name": self.trade_name,
            "status": self.status,
            "is_active": self.is_active,
            # Without this a consumer sees is_active false and has to parse the
            # status string to tell "Inactive" from "Cancelled suo-moto".
            "is_cancelled": self.is_cancelled,
            "constitution": self.constitution,
            "taxpayer_type": self.taxpayer_type,
            "registration_date": self.registration_date.isoformat()
            if self.registration_date
            else None,
            "cancellation_date": self.cancellation_date.isoformat()
            if self.cancellation_date
            else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "nature_of_business": list(self.nature_of_business),
            "principal_address": self.principal_address.as_line()
            if self.principal_address
            else None,
            "additional_addresses": [address.as_line() for address in self.additional_addresses],
            "central_jurisdiction": str(self.central_jurisdiction) or None,
            "state_jurisdiction": str(self.state_jurisdiction) or None,
            "einvoice_enabled": self.einvoice_enabled,
            "is_field_visit_conducted": self.is_field_visit_conducted,
            "core_business_activity": self.core_business_activity,
            "aadhaar_verified": self.aadhaar_verified,
            "aadhaar_verified_on": (
                self.aadhaar_verified_on.isoformat() if self.aadhaar_verified_on else None
            ),
            "ekyc_status": self.ekyc_status,
            "composition_rate": self.composition_rate,
            "extra": self.unmapped,
        }

    @property
    def unmapped(self) -> dict[str, Any]:
        """Portal keys this class does not model, so nothing is silently lost."""
        return {key: value for key, value in self.raw.items() if key not in _MAPPED_KEYS}

    def __str__(self) -> str:
        return f"{self.name or self.gstin} ({self.status or 'unknown status'})"


@dataclass(frozen=True, slots=True)
class GoodsOrService:
    """One HSN (goods) or SAC (services) entry the taxpayer is registered for."""

    code: str | None = None
    description: str | None = None
    is_service: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``bzsdtls`` entry (``saccd``/``sdes`` or ``hsncd``/``gdes``)."""
        sac = as_text(payload.get("saccd"))
        return cls(
            code=sac or as_text(payload.get("hsncd")),
            description=as_text(payload.get("sdes")) or as_text(payload.get("gdes")),
            is_service=sac is not None,
        )

    def __str__(self) -> str:
        return " - ".join(part for part in (self.code, self.description) if part)


@dataclass(frozen=True, slots=True)
class FinancialYear:
    """A financial year for which returns can be listed."""

    label: str
    value: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``dropdownfinyear`` entry (``year``/``value``)."""
        return cls(label=str(payload.get("year", "")), value=str(payload.get("value", "")))

    @property
    def start_year(self) -> int | None:
        return int(self.value) if self.value.isdigit() else None

    def __str__(self) -> str:
        return self.label or self.value


@dataclass(frozen=True, slots=True)
class FilingPreference:
    """Return-filing frequency the taxpayer opted for in a quarter."""

    quarter: str
    preference: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``taxpayerProfileDetails`` entry (``quarter``/``preference``)."""
        return cls(
            quarter=str(payload.get("quarter", "")),
            preference=str(payload.get("preference", "")),
        )

    @property
    def is_quarterly(self) -> bool:
        return self.preference.upper() == "Q"

    @property
    def is_monthly(self) -> bool:
        return self.preference.upper() == "M"

    def __str__(self) -> str:
        frequency = (
            "quarterly" if self.is_quarterly else "monthly" if self.is_monthly else self.preference
        )
        return f"{self.quarter}: {frequency}"


@dataclass(frozen=True, slots=True)
class TaxpayerProfile:
    """Everything the public portal exposes for one GSTIN.

    Only :attr:`details` costs a captcha; the other three come from
    captcha-free endpoints on the same session.
    """

    details: TaxpayerDetails
    goods_and_services: tuple[GoodsOrService, ...] = ()
    financial_years: tuple[FinancialYear, ...] = ()
    filing_preferences: tuple[FilingPreference, ...] = ()

    @property
    def gstin(self) -> str:
        """Shortcut for ``details.gstin``."""
        return self.details.gstin

    @property
    def name(self) -> str | None:
        """Shortcut for ``details.name``: trade name, else legal name."""
        return self.details.name

    @property
    def is_active(self) -> bool:
        """Shortcut for ``details.is_active``."""
        return self.details.is_active

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready view of the whole profile."""
        return {
            **self.details.as_dict(),
            "goods_and_services": [
                {"code": item.code, "description": item.description, "is_service": item.is_service}
                for item in self.goods_and_services
            ],
            "financial_years": [year.label for year in self.financial_years],
            "filing_preferences": [
                {"quarter": item.quarter, "preference": item.preference}
                for item in self.filing_preferences
            ],
        }

    def __str__(self) -> str:
        return str(self.details)
