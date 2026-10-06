"""Validate GSTINs and fetch taxpayer details from the Indian GST portal."""

from .bulk import ValidationResult, enrich_many, validate_many
from .cache import DEFAULT_CACHE, DiskCache, NullCache, TaxpayerCache, TTLCache
from .cli import main
from .client import AsyncGSTClient, GSTClient
from .exceptions import (
    CaptchaError,
    GSTValidatorError,
    InvalidGSTINError,
    InvalidPANError,
    TaxpayerLookupError,
)
from .gstin import validate_pan
from .models import (
    GSTIN,
    Address,
    Captcha,
    FilingPreference,
    FinancialYear,
    GoodsOrService,
    GSTINLayout,
    Jurisdiction,
    Registration,
    TaxpayerDetails,
    TaxpayerProfile,
)
from .search import (
    ApplicationStatus,
    CompositionTaxpayer,
    GSTPractitioner,
    HSNCode,
    ReferenceNumber,
    TemporaryRegistration,
)

__all__ = [
    "DEFAULT_CACHE",
    "GSTIN",
    "Address",
    "ApplicationStatus",
    "AsyncGSTClient",
    "Captcha",
    "CaptchaError",
    "CompositionTaxpayer",
    "DiskCache",
    "FilingPreference",
    "FinancialYear",
    "GSTClient",
    "GSTINLayout",
    "GSTPractitioner",
    "GSTValidatorError",
    "GoodsOrService",
    "HSNCode",
    "InvalidGSTINError",
    "InvalidPANError",
    "Jurisdiction",
    "NullCache",
    "ReferenceNumber",
    "Registration",
    "TTLCache",
    "TaxpayerCache",
    "TaxpayerDetails",
    "TaxpayerLookupError",
    "TaxpayerProfile",
    "TemporaryRegistration",
    "ValidationResult",
    "enrich_many",
    "main",
    "validate_many",
    "validate_pan",
]
__version__ = "0.5.2"  # x-release-please-version
