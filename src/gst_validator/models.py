"""Value objects returned by :mod:`gst_validator`."""

import base64
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Self, cast

from .exceptions import InvalidGSTINError

__all__ = [
    "GSTIN",
    "Address",
    "Captcha",
    "FilingPreference",
    "FinancialYear",
    "GoodsOrService",
    "Jurisdiction",
    "TaxpayerDetails",
    "TaxpayerProfile",
]

_GSTIN_PATTERN: Final = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHECKSUM_ALPHABET: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DATE_FORMATS: Final = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")

# https://en.wikipedia.org/wiki/List_of_GST_state_codes - first two GSTIN digits.
_STATE_NAMES: Final[dict[str, str]] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (old)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}
# ``ntcrbs`` ships as a short code.
_CORE_BUSINESS: Final[dict[str, str]] = {
    "SPO": "Supplier of Services",
    "MFR": "Manufacturer",
    "TRD": "Trader",
    "RTL": "Retailer",
    "WHL": "Wholesaler",
    "OTH": "Others",
}

# 4th PAN character encodes the holder type.
_PAN_ENTITY_TYPES: Final[dict[str, str]] = {
    "A": "Association of Persons",
    "B": "Body of Individuals",
    "C": "Company",
    "F": "Firm / LLP",
    "G": "Government",
    "H": "Hindu Undivided Family",
    "J": "Artificial Juridical Person",
    "L": "Local Authority",
    "P": "Individual",
    "T": "Trust",
}


def _text(value: object) -> str | None:
    """Portal payloads use ``null``, ``""`` and ``"NA"`` interchangeably."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return None if cleaned.upper() in {"", "NA", "NULL", "-"} else cleaned


def _parse_date(value: object) -> date | None:
    raw = _text(value)
    if raw is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _mapping(value: object) -> dict[str, Any]:
    """Narrow an arbitrary JSON value to a string-keyed mapping."""
    if not isinstance(value, dict):
        return {}
    # The cast is redundant for mypy but tells pyright the keys/values are Any
    # rather than Unknown, which strict mode reports.
    items = cast(dict[Any, Any], value)  # type: ignore[redundant-cast]
    return {str(key): item for key, item in items.items()}


def _sequence(value: object) -> list[Any]:
    """Narrow an arbitrary JSON value to a list."""
    if not isinstance(value, list):
        return []
    return cast(list[Any], value)  # type: ignore[redundant-cast]


@dataclass(frozen=True, slots=True)
class GSTIN:
    """A validated 15-character GST Identification Number."""

    value: str

    def __post_init__(self) -> None:
        if not _GSTIN_PATTERN.fullmatch(self.value):
            raise InvalidGSTINError(self.value, "does not match the GSTIN format")
        if self.value[-1] != self._checksum(self.value[:14]):
            raise InvalidGSTINError(self.value, "checksum digit mismatch")

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Normalise ``raw`` (strip, upper-case) and validate it."""
        return cls(raw.strip().upper())

    @classmethod
    def is_valid(cls, raw: str) -> bool:
        """Non-raising variant of :meth:`parse`."""
        try:
            cls.parse(raw)
        except InvalidGSTINError:
            return False
        return True

    @staticmethod
    def _checksum(first_fourteen: str) -> str:
        """Compute the GSTIN check character (mod-36 weighted sum)."""
        total = 0
        for position, character in enumerate(first_fourteen):
            product = _CHECKSUM_ALPHABET.index(character) * (1 + position % 2)
            total += product // 36 + product % 36
        return _CHECKSUM_ALPHABET[(36 - total % 36) % 36]

    @property
    def state_code(self) -> str:
        return self.value[:2]

    @property
    def state_name(self) -> str | None:
        return _STATE_NAMES.get(self.state_code)

    @property
    def pan(self) -> str:
        return self.value[2:12]

    @property
    def entity_type(self) -> str | None:
        """Entity class encoded in the 4th PAN character."""
        return _PAN_ENTITY_TYPES.get(self.pan[3])

    @property
    def registration_sequence(self) -> str:
        """13th character: the Nth registration of this PAN in this state."""
        return self.value[12]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Captcha:
    """Captcha image bound to the client session that fetched it."""

    content: bytes
    media_type: str = "image/png"

    @property
    def base64(self) -> str:
        """Bare base64 payload - hand this to a human or a solver service."""
        return base64.b64encode(self.content).decode("ascii")

    @property
    def data_uri(self) -> str:
        """``data:`` URI, drop-in for an ``<img src=...>`` in a web UI."""
        return f"data:{self.media_type};base64,{self.base64}"

    def save(self, path: str) -> None:
        """Write the raw image bytes to ``path``."""
        with open(path, "wb") as handle:
            handle.write(self.content)


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
        addr = _mapping(payload.get("addr")) or payload
        nature = _text(payload.get("ntr")) or _text(addr.get("ntr"))
        return cls(
            floor=_text(addr.get("flno")),
            building_number=_text(addr.get("bno")),
            building_name=_text(addr.get("bnm")),
            street=_text(addr.get("st")),
            location=_text(addr.get("loc")),
            landmark=_text(addr.get("landMark")),
            city=_text(addr.get("city")),
            district=_text(addr.get("dst")),
            state=_text(addr.get("stcd")),
            pincode=_text(addr.get("pncd")),
            latitude=_text(addr.get("lt")),
            longitude=_text(addr.get("lg")),
            nature_of_business=tuple(part.strip() for part in nature.split(",")) if nature else (),
            full=_text(addr.get("adr")) or _text(payload.get("adr")),
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
    constitution: str | None = None
    taxpayer_type: str | None = None
    registration_date: date | None = None
    cancellation_date: date | None = None
    last_updated: date | None = None
    nature_of_business: tuple[str, ...] = ()
    principal_address: Address | None = None
    additional_addresses: tuple[Address, ...] = ()
    central_jurisdiction: Jurisdiction = field(default_factory=Jurisdiction)
    state_jurisdiction: Jurisdiction = field(default_factory=Jurisdiction)
    einvoice_enabled: bool | None = None
    is_field_visit_conducted: bool | None = None
    core_business_activity: str | None = None
    aadhaar_verified: bool | None = None
    aadhaar_verified_on: date | None = None
    ekyc_status: str | None = None
    composition_rate: str | None = None
    raw: dict[str, Any] = field(repr=False, default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from the portal's ``taxpayerDetails`` JSON body."""
        nature = _sequence(payload.get("nba"))
        additional = _sequence(payload.get("adadr"))
        principal = _mapping(payload.get("pradr"))
        return cls(
            gstin=str(payload.get("gstin", "")),
            legal_name=_text(payload.get("lgnm")),
            trade_name=_text(payload.get("tradeNam")),
            status=_text(payload.get("sts")),
            constitution=_text(payload.get("ctb")),
            taxpayer_type=_text(payload.get("dty")),
            registration_date=_parse_date(payload.get("rgdt")),
            cancellation_date=_parse_date(payload.get("cxdt")),
            last_updated=_parse_date(payload.get("lstupdt")),
            nature_of_business=tuple(str(item) for item in nature),
            principal_address=Address.from_payload(principal) if principal else None,
            additional_addresses=tuple(
                Address.from_payload(mapped) for entry in additional if (mapped := _mapping(entry))
            ),
            central_jurisdiction=Jurisdiction(
                office=_text(payload.get("ctj")), code=_text(payload.get("ctjCd"))
            ),
            state_jurisdiction=Jurisdiction(
                office=_text(payload.get("stj")), code=_text(payload.get("stjCd"))
            ),
            einvoice_enabled=_flag(payload.get("einvoiceStatus")),
            is_field_visit_conducted=_flag(payload.get("isFieldVisitConducted")),
            core_business_activity=_CORE_BUSINESS.get(
                _text(payload.get("ntcrbs")) or "", _text(payload.get("ntcrbs"))
            ),
            aadhaar_verified=_flag(payload.get("adhrVFlag")),
            aadhaar_verified_on=_parse_date(payload.get("adhrVdt")),
            ekyc_status=_text(payload.get("ekycVFlag")),
            composition_rate=_text(payload.get("cmpRt")),
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
        return (self.status or "").casefold() == "active"

    @property
    def is_cancelled(self) -> bool:
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
            "field_visit_conducted": self.is_field_visit_conducted,
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


def _flag(value: object) -> bool | None:
    """Portal booleans arrive as ``true``/``"Yes"``/``"Y"``/``"No"``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"yes", "y", "true", "1"}


@dataclass(frozen=True, slots=True)
class GoodsOrService:
    """One HSN (goods) or SAC (services) entry the taxpayer is registered for."""

    code: str | None = None
    description: str | None = None
    is_service: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``bzsdtls`` entry (``saccd``/``sdes`` or ``hsncd``/``gdes``)."""
        sac = _text(payload.get("saccd"))
        return cls(
            code=sac or _text(payload.get("hsncd")),
            description=_text(payload.get("sdes")) or _text(payload.get("gdes")),
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
        return self.details.gstin

    @property
    def name(self) -> str | None:
        return self.details.name

    @property
    def is_active(self) -> bool:
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
