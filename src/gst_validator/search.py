"""Result types for the portal's other public searches.

These sit beside :mod:`gst_validator.taxpayer`, which models one taxpayer in
depth. Each type here is the answer to a different question the portal
answers without a login: which commodity a code names, who opted into the
composition scheme, where an application has reached, whether a notice is
genuine, and who is registered to practise.

None of them keeps a ``raw`` copy of the body. ``TaxpayerDetails`` keeps one
because the disk cache round-trips it and must survive the models growing;
nothing caches these, every key the portal sends has a field, and ``unmapped``
carries anything new.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Self

from ._parsing import as_date, as_mapping, as_text
from .exceptions import InvalidGSTINError
from .gstin import GSTIN

__all__ = [
    "ApplicationStatus",
    "CompositionTaxpayer",
    "GSTPractitioner",
    "HSNCode",
    "ReferenceNumber",
    "TemporaryRegistration",
]


def _unmapped(payload: dict[str, Any], known: frozenset[str]) -> dict[str, Any]:
    """Keys the portal sent that the model does not name."""
    return {key: value for key, value in payload.items() if key not in known}


_HSN_KEYS: Final = frozenset({"c", "n"})


@dataclass(frozen=True, slots=True)
class HSNCode:
    """One HSN (goods) or SAC (services) code and its official description.

    The same shape covers both: an HSN is a commodity code, a SAC a service
    code, and the portal's search returns them from one endpoint. ``is_service``
    reports which, from the leading digits rather than from a flag the portal
    does not send.
    """

    code: str
    description: str | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from one ``qsearch`` entry (``c``/``n``)."""
        return cls(
            code=as_text(payload.get("c")) or "",
            description=as_text(payload.get("n")),
            unmapped=_unmapped(payload, _HSN_KEYS),
        )

    @property
    def is_service(self) -> bool:
        """SAC codes for services are the 99xxxx range; everything else is goods.

        Derived, not sent: the search endpoint returns goods and services in
        one list with no field telling them apart.
        """
        return self.code.startswith("99")

    @property
    def chapter(self) -> str | None:
        """The two-digit chapter a goods code belongs to."""
        return self.code[:2] if len(self.code) >= 2 else None

    def __str__(self) -> str:
        return " - ".join(part for part in (self.code, self.description) if part)


_COMPOSITION_KEYS: Final = frozenset({"gstin", "lgnm", "tradeNam", "stcd", "dtyp", "rgdt", "appdt"})


@dataclass(frozen=True, slots=True)
class CompositionTaxpayer:
    """A taxpayer who opted into, or out of, the composition scheme.

    A composition dealer pays tax at a flat rate and **cannot charge you GST
    you are able to reclaim**, so finding a supplier on this list is a
    practical matter, not a curiosity.
    """

    gstin: str
    legal_name: str | None = None
    trade_name: str | None = None
    state_code: str | None = None
    taxpayer_type: str | None = None
    registration_date: date | None = None
    applicable_from: date | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from one ``opteddata`` entry."""
        return cls(
            gstin=as_text(payload.get("gstin")) or "",
            legal_name=as_text(payload.get("lgnm")),
            trade_name=as_text(payload.get("tradeNam")),
            state_code=as_text(payload.get("stcd")),
            taxpayer_type=as_text(payload.get("dtyp")),
            registration_date=as_date(payload.get("rgdt")),
            applicable_from=as_date(payload.get("appdt")),
            unmapped=_unmapped(payload, _COMPOSITION_KEYS),
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

    def __str__(self) -> str:
        return f"{self.gstin} ({self.name or 'unknown name'})"


_ARN_KEYS: Final = frozenset(
    {"arn", "status", "stsDesc", "submissionDate", "subDate", "modDate", "formNo", "formDesc"}
)


@dataclass(frozen=True, slots=True)
class ApplicationStatus:
    """Where a GST application has reached, by its ARN.

    An ARN is the receipt for any application to the department: a new
    registration, an amendment, a refund, a cancellation.
    """

    arn: str
    status: str | None = None
    status_description: str | None = None
    form: str | None = None
    form_description: str | None = None
    submitted_on: date | None = None
    updated_on: date | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``trackarn`` body."""
        return cls(
            arn=as_text(payload.get("arn")) or "",
            status=as_text(payload.get("status")),
            status_description=as_text(payload.get("stsDesc")),
            form=as_text(payload.get("formNo")),
            form_description=as_text(payload.get("formDesc")),
            submitted_on=as_date(payload.get("submissionDate") or payload.get("subDate")),
            updated_on=as_date(payload.get("modDate")),
            unmapped=_unmapped(payload, _ARN_KEYS),
        )

    def __str__(self) -> str:
        return f"{self.arn} ({self.status_description or self.status or 'unknown status'})"


_RFN_KEYS: Final = frozenset({"rfn", "refId", "valid", "isValid", "issueDate", "docType", "office"})


@dataclass(frozen=True, slots=True)
class ReferenceNumber:
    """Whether a document reference number was really issued by the department.

    Fake GST notices are a known problem. An RFN printed on a genuine notice
    verifies here; one that does not verify did not come from the department.
    """

    reference: str
    is_genuine: bool = False
    document_type: str | None = None
    issued_on: date | None = None
    office: str | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``verifyRfn`` body."""
        flag = payload.get("valid")
        if flag is None:
            flag = payload.get("isValid")
        return cls(
            reference=as_text(payload.get("rfn")) or as_text(payload.get("refId")) or "",
            # A reference the portal knows is a reference it issued. Anything
            # it does not recognise is reported as not genuine, never as an
            # error, because "we have never seen this" is the useful answer.
            is_genuine=bool(flag) if flag is not None else bool(payload.get("docType")),
            document_type=as_text(payload.get("docType")),
            issued_on=as_date(payload.get("issueDate")),
            office=as_text(payload.get("office")),
            unmapped=_unmapped(payload, _RFN_KEYS),
        )

    def __str__(self) -> str:
        verdict = "issued by the department" if self.is_genuine else "not recognised"
        return f"{self.reference}: {verdict}"


_GSTP_KEYS: Final = frozenset(
    {"enrlNo", "trpNam", "stCd", "dstCd", "pinCd", "ctgry", "adrs", "status"}
)

# Keys the portal sends that this class drops on purpose, listed so that
# `unmapped` stays honest: it means "the portal grew a field", not "we quietly
# decided against this one". `cntctNo` and `emailId` are an individual's
# personal phone number and email address; a library that lifts them into a
# CSV column is a contact-harvesting tool, and engaging a practitioner is done
# through the portal anyway. The other two are request echoes.
_GSTP_DROPPED: Final = frozenset({"cntctNo", "emailId", "searchType", "authTokn"})


@dataclass(frozen=True, slots=True)
class GSTPractitioner:
    """A registered GST practitioner, from the portal's public directory.

    **This is a named private individual.** The portal publishes a contact
    number, an email address and a working address so that a taxpayer can
    engage a practitioner. Treat it as personal data: it is covered by the
    same rule as taxpayer data in this project's security policy, and it does
    not belong in issues, fixtures or a dataset you redistribute.
    """

    enrolment_number: str
    name: str | None = None
    state_code: str | None = None
    district_code: str | None = None
    pincode: str | None = None
    category: str | None = None
    address: str | None = None
    status: str | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from one ``search/gstp`` entry.

        The contact number and email the portal returns are dropped, not
        modelled: see ``_GSTP_DROPPED`` for why. They are therefore absent
        from :attr:`unmapped` too, which reports portal *changes*.
        """
        address = as_mapping(payload.get("adrs"))
        return cls(
            enrolment_number=as_text(payload.get("enrlNo")) or "",
            name=as_text(payload.get("trpNam")),
            state_code=as_text(payload.get("stCd")),
            district_code=as_text(payload.get("dstCd")),
            pincode=as_text(payload.get("pinCd")) or as_text(address.get("pinCd")),
            category=as_text(payload.get("ctgry")),
            address=as_text(address.get("addr")),
            status=as_text(payload.get("status")),
            unmapped=_unmapped(payload, _GSTP_KEYS | _GSTP_DROPPED),
        )

    @property
    def is_active(self) -> bool:
        """The portal reports "A" for an active enrolment."""
        return (self.status or "").strip().upper() == "A"

    def __str__(self) -> str:
        return f"{self.enrolment_number} ({self.name or 'unknown name'})"


_TEMP_KEYS: Final = frozenset({"tempId", "lgnm", "stateCd", "status", "regDate", "tmpIdStatus"})


@dataclass(frozen=True, slots=True)
class TemporaryRegistration:
    """A temporary registration, held by a casual or unregistered person.

    Looking one up needs either the temporary id or the registrant's own
    mobile number, so this answers "where has my application reached" rather
    than "who is this supplier".
    """

    temporary_id: str
    legal_name: str | None = None
    state_code: str | None = None
    status: str | None = None
    registered_on: date | None = None
    unmapped: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from a ``search/smreg`` body."""
        return cls(
            temporary_id=as_text(payload.get("tempId")) or "",
            legal_name=as_text(payload.get("lgnm")),
            state_code=as_text(payload.get("stateCd")),
            status=as_text(payload.get("status")) or as_text(payload.get("tmpIdStatus")),
            registered_on=as_date(payload.get("regDate")),
            unmapped=_unmapped(payload, _TEMP_KEYS),
        )

    def __str__(self) -> str:
        return f"{self.temporary_id} ({self.legal_name or 'unknown name'})"
