"""Normalising the portal's JSON, which is loose about types and emptiness."""

from datetime import date, datetime
from typing import Any, Final, cast

__all__ = ["as_date", "as_flag", "as_mapping", "as_sequence", "as_text"]

_DATE_FORMATS: Final = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")


def as_text(value: object) -> str | None:
    """Portal payloads use ``null``, ``""`` and ``"NA"`` interchangeably."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return None if cleaned.upper() in {"", "NA", "NULL", "-"} else cleaned


def as_date(value: object) -> date | None:
    """Parse a portal date. Naive on purpose: these are calendar dates."""
    raw = as_text(value)
    if raw is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            # Naive on purpose: a registration date is a calendar date, with
            # no time or zone attached to it.
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def as_mapping(value: object) -> dict[str, Any]:
    """Narrow an arbitrary JSON value to a string-keyed mapping."""
    if not isinstance(value, dict):
        return {}
    # The cast is redundant for mypy but tells pyright the keys/values are Any
    # rather than Unknown, which strict mode reports.
    items = cast("dict[Any, Any]", value)  # type: ignore[redundant-cast]
    return {str(key): item for key, item in items.items()}


def as_sequence(value: object) -> list[Any]:
    """Narrow an arbitrary JSON value to a list."""
    if not isinstance(value, list):
        return []
    return cast("list[Any]", value)  # type: ignore[redundant-cast]


def as_flag(value: object) -> bool | None:
    """Portal booleans arrive as ``true``/``"Yes"``/``"Y"``/``"No"``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"yes", "y", "true", "1"}
