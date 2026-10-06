"""The GST Identification Number itself, in each of the layouts the portal issues."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from .exceptions import InvalidGSTINError, InvalidPANError

__all__ = ["GSTIN", "GSTINLayout", "validate_pan"]

# The 14th character is "Z" for an ordinary registration, but "D" for a TDS
# deductor under section 51 and "C" for a TCS collector under section 52, so
# it cannot be pinned to "Z": that rejected valid government and e-commerce
# registrations outright. The checksum remains the real guard against typos.
# Characters 3-12 hold the holder's PAN (AAAAA9999A). The portal's own
# registration guide says a deductor without a PAN may register against its TAN
# (AAAA99999A) instead - "TDS applicants who do not have a PAN can select TAN
# and enter their TAN" - so that shape is accepted as well. Every deductor
# registration seen on the live portal so far is PAN-based (Indian Railways,
# NTPC, Indian Oil all use theirs), so the TAN layout rests on the
# documentation rather than on an observed number.
_PAN_SHAPE: Final = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_GSTIN_PATTERN: Final = re.compile(
    r"^[0-9]{2}(?:[A-Z]{5}[0-9]{4}[A-Z]|[A-Z]{4}[0-9]{5}[A-Z])[1-9A-Z][A-Z][0-9A-Z]$"
)

# Two layouts share a shape that is nothing like the PAN-based one: four
# digits, three letters, five digits, two letters, then the check digit.
#
#   9917USA29016OS6  GoDaddy, a non-resident provider of online services:
#                    "99", year 17, country USA, serial 29016, "OS" for OIDAR.
#   2317UNO00001UND  UNICEF India, a UIN holder: state 23, year 17, "UNO",
#                    serial 00001, "UN" for a United Nations body.
#
# The mod-36 check digit is computed the same way for both.
_SPECIAL_PATTERN: Final = re.compile(r"^[0-9]{4}[A-Z]{3}[0-9]{5}[A-Z]{2}[0-9A-Z]$")
_CHECKSUM_ALPHABET: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# First two GSTIN digits. Checked against the official master codes published
# on the NIC e-invoice portal, https://einvoice1.gst.gov.in/Others/MasterCodes
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
    # 28 was Andhra Pradesh before the Telangana split and is no longer
    # issued, but registrations from that period still carry it.
    "28": "Andhra Pradesh (retired)",
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
    "96": "Other Countries",
    "97": "Other Territory",
    # Also the prefix of the non-resident layout, hence GSTINLayout.NON_RESIDENT.
    "99": "Other Countries",
}
# Union territories, per the "u" flag on the portal's own state master
# (https://services.gst.gov.in/master/allstates?includeCbic=true).
#
# Delhi (07) and Puducherry (34) are deliberately absent. Both are union
# territories constitutionally, but both have a legislature and are treated as
# states for GST, which is why the portal does not flag them either.
_UNION_TERRITORIES: Final[frozenset[str]] = frozenset({"04", "25", "26", "31", "35", "38"})

# 14th GSTIN character: the kind of registration.
_REGISTRATION_TYPES: Final[dict[str, str]] = {
    "Z": "Regular",
    "D": "TDS deductor",
    "C": "TCS collector",
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


def validate_pan(value: str) -> str:
    """Normalise and validate a standalone PAN, returning the clean form.

    The PAN search costs a solved captcha, so the shape is checked before one
    is spent. Raises :class:`InvalidPANError` rather than returning a flag,
    matching :meth:`GSTIN.parse`.
    """
    cleaned = value.strip().upper()
    if not _PAN_SHAPE.fullmatch(cleaned):
        raise InvalidPANError(value, "does not match the PAN format")
    return cleaned


class GSTINLayout(StrEnum):
    """Which of the three layouts a GSTIN uses.

    The portal issues more than one shape, and most of :class:`GSTIN`'s
    properties only apply to one of them, so this is the value to branch on::

        match gstin.layout:
            case GSTINLayout.PAN:
                use(gstin.pan)
            case GSTINLayout.NON_RESIDENT | GSTINLayout.UIN:
                use(gstin.holder_code)
    """

    PAN = "pan"
    """The ordinary layout: state, PAN or TAN, sequence, type, check digit."""

    NON_RESIDENT = "non_resident"
    """An overseas provider of online services, prefixed "99"."""

    UIN = "uin"
    """A Unique Identity Number held by a UN body or a diplomatic mission."""


@dataclass(frozen=True, slots=True)
class GSTIN:
    """A validated 15-character GST Identification Number."""

    value: str

    def __post_init__(self) -> None:
        if not (_GSTIN_PATTERN.fullmatch(self.value) or _SPECIAL_PATTERN.fullmatch(self.value)):
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
    def layout(self) -> GSTINLayout:
        """Which shape this GSTIN uses, and so which properties apply."""
        if not _SPECIAL_PATTERN.fullmatch(self.value):
            return GSTINLayout.PAN
        return GSTINLayout.NON_RESIDENT if self.value[:2] == "99" else GSTINLayout.UIN

    @property
    def _is_special(self) -> bool:
        """True for either of the two layouts that carry no PAN."""
        return self.layout is not GSTINLayout.PAN

    @property
    def is_non_resident(self) -> bool:
        """True for the OIDAR layout issued to overseas service providers."""
        return self.layout is GSTINLayout.NON_RESIDENT

    @property
    def is_uin(self) -> bool:
        """True for a Unique Identity Number, held by UN bodies and embassies."""
        return self.layout is GSTINLayout.UIN

    @property
    def holder_code(self) -> str | None:
        """Characters 5-7 of either special layout.

        A country for a non-resident provider ("USA"), or the body's code for
        a UIN ("UNO"). ``None`` for the ordinary layout.
        """
        return self.value[4:7] if self._is_special else None

    @property
    def state_code(self) -> str:
        """The first two characters. "99" marks a non-resident rather than a state."""
        return self.value[:2]

    @property
    def state_name(self) -> str | None:
        """``None`` for a non-resident, which carries a country rather than a state."""
        return None if self.is_non_resident else _STATE_NAMES.get(self.state_code)

    @property
    def is_union_territory(self) -> bool:
        """Whether the state code is a union territory.

        ``False`` for Delhi and Puducherry, which are union territories with a
        legislature and are treated as states for GST, and ``False`` for the
        non-resident layout, which carries no state at all.
        """
        return self.state_code in _UNION_TERRITORIES

    @property
    def registration_year(self) -> int | None:
        """Characters 3-4 of either special layout, as a four-digit year."""
        return 2000 + int(self.value[2:4]) if self._is_special else None

    @property
    def identifier(self) -> str | None:
        """Characters 3-12: a PAN, or a TAN for a deductor registered without one.

        ``None`` for the non-resident and UIN layouts, which carry neither.
        """
        return None if self._is_special else self.value[2:12]

    @property
    def identifier_type(self) -> str | None:
        """``"PAN"`` or ``"TAN"``, by shape; ``None`` for the two special layouts."""
        identifier = self.identifier
        if identifier is None:
            return None
        return "PAN" if _PAN_SHAPE.fullmatch(identifier) else "TAN"

    @property
    def pan(self) -> str | None:
        """The holder's PAN, or ``None`` when the GSTIN embeds a TAN instead."""
        return self.identifier if self.identifier_type == "PAN" else None

    @property
    def tan(self) -> str | None:
        """The deductor's TAN, or ``None`` for an ordinary PAN-based GSTIN."""
        return self.identifier if self.identifier_type == "TAN" else None

    @property
    def entity_type(self) -> str | None:
        """Entity class encoded in the 4th PAN character; ``None`` for a TAN."""
        pan = self.pan
        return _PAN_ENTITY_TYPES.get(pan[3]) if pan is not None else None

    @property
    def registration_sequence(self) -> str | None:
        """13th character: the Nth registration of this PAN in this state.

        ``None`` for the non-resident and UIN layouts, which have no sequence.
        """
        return None if self._is_special else self.value[12]

    @property
    def registration_type(self) -> str | None:
        """14th character: "Z" ordinarily, "D" for TDS, "C" for TCS.

        Both exceptions are confirmed against the live portal: Amazon Seller
        Services holds "C" registrations beside ordinary "Z" ones, and NTPC and
        Indian Oil hold "D" registrations beside theirs. An unrecognised letter
        is left unlabelled rather than guessed at.
        """
        return None if self._is_special else _REGISTRATION_TYPES.get(self.value[13])

    @property
    def is_regular(self) -> bool:
        """False for a UIN, a non-resident, a TDS deductor or a TCS collector."""
        return not self._is_special and self.value[13] == "Z"

    def __str__(self) -> str:
        return self.value
