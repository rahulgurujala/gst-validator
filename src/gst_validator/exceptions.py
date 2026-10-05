"""Exception hierarchy for :mod:`gst_validator`."""

__all__ = [
    "CaptchaError",
    "GSTValidatorError",
    "InvalidGSTINError",
    "TaxpayerLookupError",
]


class GSTValidatorError(Exception):
    """Base class for every error raised by this package."""


class InvalidGSTINError(GSTValidatorError, ValueError):
    """Raised when a string is not a structurally valid GSTIN."""

    def __init__(self, value: str, reason: str) -> None:
        super().__init__(f"invalid GSTIN {value!r}: {reason}")
        self.value: str = value
        self.reason: str = reason


class CaptchaError(GSTValidatorError):
    """Raised when the captcha could not be fetched."""


class TaxpayerLookupError(GSTValidatorError):
    """Raised when the taxpayer details request fails or is rejected.

    ``code`` carries the portal's ``errorCode`` when the rejection came from
    the portal itself rather than from the transport.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message if code is None else f"{message} [{code}]")
        self.code: str | None = code
