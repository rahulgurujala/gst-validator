"""Checking many GSTINs at once.

Offline validation costs nothing, so a whole column of a spreadsheet can be
checked without touching the network. Enrichment adds the three endpoints that
need no captcha; the captcha-gated taxpayer lookup is deliberately not part of
this, because each one costs a person solving an image.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from .client import GSTClient
from .exceptions import GSTValidatorError, InvalidGSTINError
from .gstin import GSTIN
from .taxpayer import FilingPreference, FinancialYear, GoodsOrService

__all__ = ["ValidationResult", "enrich_many", "validate_many"]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """One row of a bulk check: what was given, and what it turned out to be.

    A row never raises. An input that cannot be parsed comes back with
    ``gstin`` unset and ``error`` saying why, so a bad row in the middle of a
    file does not stop the rest.
    """

    value: str
    """The input exactly as it was given, before stripping or upper-casing."""

    gstin: GSTIN | None = None
    error: str | None = None
    goods_and_services: tuple[GoodsOrService, ...] = ()
    financial_years: tuple[FinancialYear, ...] = ()
    filing_preferences: tuple[FilingPreference, ...] = ()
    enrichment_error: str | None = None
    """Why the captcha-free lookups failed, when the GSTIN itself was fine."""

    extra: dict[str, str] = field(default_factory=dict[str, str])
    """Other columns from the input row, carried through to the output."""

    @property
    def is_valid(self) -> bool:
        return self.gstin is not None

    def as_dict(self) -> dict[str, Any]:
        """Flat, JSON- and CSV-friendly. Keys are stable across rows."""
        gstin = self.gstin
        row: dict[str, Any] = {
            **self.extra,
            "input": self.value,
            "valid": self.is_valid,
            "error": self.error,
            "gstin": gstin.value if gstin else None,
            "state_code": gstin.state_code if gstin else None,
            "state_name": gstin.state_name if gstin else None,
            "identifier": gstin.identifier if gstin else None,
            "identifier_type": gstin.identifier_type if gstin else None,
            "pan": gstin.pan if gstin else None,
            "tan": gstin.tan if gstin else None,
            "entity_type": gstin.entity_type if gstin else None,
            "registration_sequence": gstin.registration_sequence if gstin else None,
            "registration_type": gstin.registration_type if gstin else None,
            "layout": gstin.layout.value if gstin else None,
        }
        if self.goods_and_services or self.financial_years or self.filing_preferences:
            row["codes"] = "; ".join(str(item) for item in self.goods_and_services)
            row["financial_years"] = "; ".join(year.label for year in self.financial_years)
            row["filing_preferences"] = "; ".join(str(p) for p in self.filing_preferences)
        if self.enrichment_error:
            row["enrichment_error"] = self.enrichment_error
        return row


def validate_many(
    values: Iterable[str], *, extras: Iterable[dict[str, str]] | None = None
) -> Iterator[ValidationResult]:
    """Validate each value offline, yielding one result per input.

    Nothing is raised for a bad row::

        for row in validate_many(["27AAACR5055K1Z7", "nope"]):
            print(row.value, row.is_valid, row.error)

    ``extras`` supplies per-row columns to carry through, such as the rest of
    a CSV record; it is zipped with ``values``.
    """
    empty: dict[str, str] = {}
    carried = iter(extras) if extras is not None else None
    for value in values:
        extra = next(carried, empty) if carried is not None else empty
        try:
            yield ValidationResult(value=value, gstin=GSTIN.parse(value), extra=extra)
        except InvalidGSTINError as error:
            yield ValidationResult(value=value, error=error.reason, extra=extra)


def enrich_many(
    results: Iterable[ValidationResult], *, client: GSTClient | None = None
) -> Iterator[ValidationResult]:
    """Add the three captcha-free endpoints to each valid result.

    One session is reused for the whole batch. A row whose lookups fail keeps
    its validation result and records the reason in
    :attr:`ValidationResult.enrichment_error`, so one failure does not end the
    run.
    """
    owned = client is None
    session = client or GSTClient()
    try:
        for result in results:
            if result.gstin is None:
                yield result
                continue
            # replace() rather than a fresh instance, so a field added to
            # ValidationResult later cannot be dropped here by omission.
            try:
                yield replace(
                    result,
                    goods_and_services=session.fetch_goods_and_services(result.gstin),
                    financial_years=session.fetch_financial_years(result.gstin),
                    filing_preferences=session.fetch_filing_preferences(result.gstin),
                )
            except GSTValidatorError as error:
                yield replace(result, enrichment_error=str(error))
    finally:
        if owned:
            session.close()
