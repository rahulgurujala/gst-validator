"""HTTP clients for the GST portal taxpayer search."""

from types import TracebackType
from typing import Any, ClassVar, Final, Self, cast

import httpx

from .cache import DEFAULT_CACHE, TaxpayerCache
from .exceptions import CaptchaError, TaxpayerLookupError
from .models import (
    GSTIN,
    Captcha,
    FilingPreference,
    FinancialYear,
    GoodsOrService,
    TaxpayerDetails,
    TaxpayerProfile,
)

__all__ = ["AsyncGSTClient", "GSTClient"]

_DEFAULT_TIMEOUT: Final = 15.0

# The portal drops connections from non-browser clients: a plain library
# User-Agent gets a bot-challenge page and the captcha request is then reset.
_DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_RETRIES: Final = 2

# Portal error codes worth explaining instead of echoing verbatim. Only codes
# whose meaning has been confirmed belong here (SWEB_9000 from live responses,
# SWEB_9035 from the portal's published error-code list); an unknown code is
# reported as-is on the exception, which beats guessing at its meaning.
_ERROR_HINTS: Final[dict[str, str]] = {
    "SWEB_9000": "invalid or expired captcha - fetch a new one from the same client",
    "SWEB_9035": "the account is locked on the portal",
}


class _BaseGSTClient:
    """Shared endpoints and payload handling for the sync/async clients.

    The portal ties a captcha to the cookies issued by the search page, so one
    client instance is one captcha session: fetch the captcha, then look up a
    GSTIN with the solved text using the *same* instance.
    """

    BASE_URL: ClassVar[str] = "https://services.gst.gov.in/services"
    SEARCH_PATH: ClassVar[str] = "/searchtp"
    CAPTCHA_PATH: ClassVar[str] = "/captcha"
    DETAILS_PATH: ClassVar[str] = "/api/search/taxpayerDetails"
    GOODS_PATH: ClassVar[str] = "/api/search/goodservice"
    FINYEAR_PATH: ClassVar[str] = "/api/dropdownfinyear"
    PROFILE_PATH: ClassVar[str] = "/api/search/taxpayerProfileDetails"

    @classmethod
    def _base_headers(cls, user_agent: str) -> dict[str, str]:
        return {
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }

    @classmethod
    def _referer(cls) -> str:
        return f"{cls.BASE_URL}{cls.SEARCH_PATH}"

    @classmethod
    def _captcha_headers(cls) -> dict[str, str]:
        return {
            "Accept": "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8",
            "Referer": cls._referer(),
        }

    @classmethod
    def _api_headers(cls) -> dict[str, str]:
        """Headers for the captcha-free GET endpoints."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": cls._referer(),
            "X-Requested-With": "XMLHttpRequest",
        }

    @classmethod
    def _details_headers(cls) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": cls._referer(),
            "Origin": "https://services.gst.gov.in",
            "X-Requested-With": "XMLHttpRequest",
        }

    @staticmethod
    def _as_captcha(response: httpx.Response) -> Captcha:
        media_type = response.headers.get("content-type", "image/png").split(";")[0]
        if not media_type.startswith("image/"):
            raise CaptchaError(f"expected an image, got {media_type!r}")
        if not response.content:
            raise CaptchaError("captcha response was empty")
        return Captcha(content=response.content, media_type=media_type)

    @staticmethod
    def _request_payload(gstin: GSTIN, captcha: str) -> dict[str, str]:
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        return {"gstin": gstin.value, "captcha": text}

    @staticmethod
    def _as_details(response: httpx.Response) -> TaxpayerDetails:
        payload = _json_object(response)
        if not payload.get("gstin"):
            raise _rejection(payload)
        return TaxpayerDetails.from_payload(payload)

    @staticmethod
    def _as_goods(response: httpx.Response) -> tuple[GoodsOrService, ...]:
        # Services come back under "bzsdtls" (saccd/sdes) and goods under
        # "bzgddtls" (hsncd/gdes); a taxpayer can be registered for either.
        payload = _json_object(response)
        entries = _entries(payload.get("bzsdtls")) + _entries(payload.get("bzgddtls"))
        return tuple(GoodsOrService.from_payload(entry) for entry in entries)

    @staticmethod
    def _as_years(response: httpx.Response) -> tuple[FinancialYear, ...]:
        return tuple(FinancialYear.from_payload(entry) for entry in _entries(_envelope(response)))

    @staticmethod
    def _as_preferences(response: httpx.Response) -> tuple[FilingPreference, ...]:
        data = _envelope(response)
        rows: object = data
        if isinstance(data, dict):
            # The quarters sit one level deeper, under "response".
            mapping = cast(dict[str, Any], data)
            rows = mapping.get("response")
        return tuple(FilingPreference.from_payload(entry) for entry in _entries(rows))


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body, or raise :class:`TaxpayerLookupError`."""
    try:
        body: object = response.json()
    except ValueError as error:
        raise TaxpayerLookupError("portal returned a non-JSON body") from error
    if not isinstance(body, dict):
        raise TaxpayerLookupError(f"unexpected payload type {type(body).__name__}")
    return cast(dict[str, Any], body)


def _envelope(response: httpx.Response) -> object:
    """Unwrap the ``{"status": 1, "data": ...}`` envelope these endpoints use."""
    payload = _json_object(response)
    if payload.get("status") not in (1, "1", None):
        raise _rejection(payload)
    return payload.get("data")


def _entries(value: object) -> list[dict[str, Any]]:
    """Coerce a JSON list into a list of string-keyed mappings."""
    if not isinstance(value, list):
        return []
    rows = cast(list[Any], value)  # type: ignore[redundant-cast]
    return [cast(dict[str, Any], row) for row in rows if isinstance(row, dict)]


def _rejection(payload: dict[str, Any]) -> TaxpayerLookupError:
    """Turn a portal payload that carries no taxpayer into an exception.

    The portal answers rejections with HTTP 200 and a body such as
    ``{"url": "/", "message": null, "errorCode": "SWEB_9000"}``, so the absence
    of ``gstin`` - not the status code - is what marks a failed lookup. Some
    endpoints nest the same fields one level down instead, as
    ``{"status": 0, "error": {"message": "...", "errorCode": "..."}}``, so both
    shapes are read before falling back to a generic message.
    """
    nested = payload.get("error")
    detail: dict[str, Any] = payload
    if isinstance(nested, dict):
        detail = cast(dict[str, Any], nested)

    raw_code: object = detail.get("errorCode") or payload.get("errorCode")
    code = str(raw_code) if raw_code is not None else None
    raw_message: object = (
        detail.get("errorMsg")
        or detail.get("message")
        or payload.get("errorMsg")
        or payload.get("message")
    )
    message = str(raw_message) if raw_message is not None else None
    if message is None:
        message = _ERROR_HINTS.get(code or "", "portal returned no taxpayer details")
    return TaxpayerLookupError(message, code=code)


class GSTClient(_BaseGSTClient):
    """Synchronous client. Use as a context manager to close the connection pool."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        user_agent: str = _DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        cache: TaxpayerCache | None = None,
    ) -> None:
        self._cache: TaxpayerCache = DEFAULT_CACHE if cache is None else cache
        self._session_ready = False
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            timeout=timeout,
            follow_redirects=True,
            headers=self._base_headers(user_agent),
            transport=transport or httpx.HTTPTransport(retries=_DEFAULT_RETRIES),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection pool; also done by ``with``."""
        self._client.close()

    def fetch_captcha(self) -> Captcha:
        """Open a portal session and download its captcha image."""
        try:
            self._client.get(self.SEARCH_PATH).raise_for_status()
            self._session_ready = True
            response = self._client.get(self.CAPTCHA_PATH, headers=self._captcha_headers())
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise CaptchaError(f"could not fetch captcha: {error}") from error
        return self._as_captcha(response)

    def cached(self, gstin: GSTIN | str) -> TaxpayerDetails | None:
        """Return a cached result for ``gstin``, without touching the network."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        return self._cache.get(number.value)

    def fetch_details(
        self, gstin: GSTIN | str, captcha: str, *, refresh: bool = False
    ) -> TaxpayerDetails:
        """Look up ``gstin`` using the text solved from :meth:`fetch_captcha`.

        A cache hit short-circuits the request, so check :meth:`cached` before
        spending a captcha. Pass ``refresh=True`` to force a fresh lookup.
        """
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        if not refresh and (hit := self._cache.get(number.value)) is not None:
            return hit
        payload = self._request_payload(number, captcha)
        try:
            response = self._client.post(
                self.DETAILS_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"lookup failed: {error}") from error
        details = self._as_details(response)
        self._cache.set(number.value, details)
        return details

    def fetch_goods_and_services(self, gstin: GSTIN | str) -> tuple[GoodsOrService, ...]:
        """HSN/SAC codes the taxpayer is registered for. No captcha needed."""
        return self._as_goods(self._get(self.GOODS_PATH, gstin))

    def fetch_financial_years(self, gstin: GSTIN | str) -> tuple[FinancialYear, ...]:
        """Financial years for which returns exist. No captcha needed."""
        return self._as_years(self._get(self.FINYEAR_PATH, gstin))

    def fetch_filing_preferences(
        self, gstin: GSTIN | str, *, toggle: str = "current"
    ) -> tuple[FilingPreference, ...]:
        """Monthly/quarterly filing preference per quarter. No captcha needed."""
        return self._as_preferences(self._get(self.PROFILE_PATH, gstin, toggle=toggle))

    def fetch_profile(
        self, gstin: GSTIN | str, captcha: str, *, refresh: bool = False
    ) -> TaxpayerProfile:
        """Everything the portal exposes: details plus the captcha-free extras."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        details = self.fetch_details(number, captcha, refresh=refresh)
        return TaxpayerProfile(
            details=details,
            goods_and_services=self.fetch_goods_and_services(number),
            financial_years=self.fetch_financial_years(number),
            filing_preferences=self.fetch_filing_preferences(number),
        )

    def _get(self, path: str, gstin: GSTIN | str, **params: str) -> httpx.Response:
        """GET a captcha-free endpoint, opening a portal session if needed."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        try:
            self._ensure_session()
            response = self._client.get(
                path, params={"gstin": number.value, **params}, headers=self._api_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {path} failed: {error}") from error
        return response

    def _ensure_session(self) -> None:
        """These endpoints need the cookies the search page hands out."""
        if not self._session_ready:
            self._client.get(self.SEARCH_PATH).raise_for_status()
            self._session_ready = True


class AsyncGSTClient(_BaseGSTClient):
    """Asynchronous counterpart of :class:`GSTClient`."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        user_agent: str = _DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: TaxpayerCache | None = None,
    ) -> None:
        self._cache: TaxpayerCache = DEFAULT_CACHE if cache is None else cache
        self._session_ready = False
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout,
            follow_redirects=True,
            headers=self._base_headers(user_agent),
            transport=transport or httpx.AsyncHTTPTransport(retries=_DEFAULT_RETRIES),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool; also done by ``async with``."""
        await self._client.aclose()

    async def fetch_captcha(self) -> Captcha:
        """Open a portal session and download its captcha image."""
        try:
            (await self._client.get(self.SEARCH_PATH)).raise_for_status()
            self._session_ready = True
            response = await self._client.get(self.CAPTCHA_PATH, headers=self._captcha_headers())
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise CaptchaError(f"could not fetch captcha: {error}") from error
        return self._as_captcha(response)

    def cached(self, gstin: GSTIN | str) -> TaxpayerDetails | None:
        """Return a cached result for ``gstin``, without touching the network."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        return self._cache.get(number.value)

    async def fetch_details(
        self, gstin: GSTIN | str, captcha: str, *, refresh: bool = False
    ) -> TaxpayerDetails:
        """Look up ``gstin`` with the text solved from :meth:`fetch_captcha`.

        A cache hit short-circuits the request; pass ``refresh=True`` to force
        a fresh lookup.
        """
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        if not refresh and (hit := self._cache.get(number.value)) is not None:
            return hit
        payload = self._request_payload(number, captcha)
        try:
            response = await self._client.post(
                self.DETAILS_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"lookup failed: {error}") from error
        details = self._as_details(response)
        self._cache.set(number.value, details)
        return details

    async def fetch_goods_and_services(self, gstin: GSTIN | str) -> tuple[GoodsOrService, ...]:
        """HSN/SAC codes the taxpayer is registered for. No captcha needed."""
        return self._as_goods(await self._get(self.GOODS_PATH, gstin))

    async def fetch_financial_years(self, gstin: GSTIN | str) -> tuple[FinancialYear, ...]:
        """Financial years for which returns exist. No captcha needed."""
        return self._as_years(await self._get(self.FINYEAR_PATH, gstin))

    async def fetch_filing_preferences(
        self, gstin: GSTIN | str, *, toggle: str = "current"
    ) -> tuple[FilingPreference, ...]:
        """Monthly/quarterly filing preference per quarter. No captcha needed."""
        return self._as_preferences(await self._get(self.PROFILE_PATH, gstin, toggle=toggle))

    async def fetch_profile(
        self, gstin: GSTIN | str, captcha: str, *, refresh: bool = False
    ) -> TaxpayerProfile:
        """Everything the portal exposes: details plus the captcha-free extras."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        details = await self.fetch_details(number, captcha, refresh=refresh)
        return TaxpayerProfile(
            details=details,
            goods_and_services=await self.fetch_goods_and_services(number),
            financial_years=await self.fetch_financial_years(number),
            filing_preferences=await self.fetch_filing_preferences(number),
        )

    async def _get(self, path: str, gstin: GSTIN | str, **params: str) -> httpx.Response:
        """GET a captcha-free endpoint, opening a portal session if needed."""
        number = gstin if isinstance(gstin, GSTIN) else GSTIN.parse(gstin)
        try:
            await self._ensure_session()
            response = await self._client.get(
                path, params={"gstin": number.value, **params}, headers=self._api_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {path} failed: {error}") from error
        return response

    async def _ensure_session(self) -> None:
        """These endpoints need the cookies the search page hands out."""
        if not self._session_ready:
            (await self._client.get(self.SEARCH_PATH)).raise_for_status()
            self._session_ready = True
