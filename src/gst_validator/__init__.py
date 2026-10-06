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
    validate_pan,
)

__all__ = [
    "DEFAULT_CACHE",
    "GSTIN",
    "Address",
    "AsyncGSTClient",
    "Captcha",
    "CaptchaError",
    "DiskCache",
    "FilingPreference",
    "FinancialYear",
    "GSTClient",
    "GSTINLayout",
    "GSTValidatorError",
    "GoodsOrService",
    "InvalidGSTINError",
    "InvalidPANError",
    "Jurisdiction",
    "NullCache",
    "Registration",
    "TTLCache",
    "TaxpayerCache",
    "TaxpayerDetails",
    "TaxpayerLookupError",
    "TaxpayerProfile",
    "ValidationResult",
    "enrich_many",
    "main",
    "validate_many",
    "validate_pan",
]
__version__ = "0.3.0"  # x-release-please-version
