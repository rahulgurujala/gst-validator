"""HTTP clients for the GST portal taxpayer search."""

from types import TracebackType
from typing import Any, ClassVar, Final, Self, cast

import httpx

from ._parsing import as_mapping
from .cache import DEFAULT_CACHE, TaxpayerCache
from .exceptions import CaptchaError, TaxpayerLookupError
from .gstin import validate_pan
from .models import (
    GSTIN,
    Captcha,
    FilingPreference,
    FinancialYear,
    GoodsOrService,
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
    PAN_SEARCH_PATH: ClassVar[str] = "/searchtpbypan"
    REGISTRATIONS_PATH: ClassVar[str] = "/api/get/gstndtls"
    COMPOSITION_PATH: ClassVar[str] = "/api/search/tplist/opteddata"
    ARN_PATH: ClassVar[str] = "/trackarn"
    PRACTITIONER_PATH: ClassVar[str] = "/api/search/gstp"
    TEMPORARY_PATH: ClassVar[str] = "/api/search/smreg"
    # These two sit outside /services, so they are requested absolutely.
    HSN_URL: ClassVar[str] = "https://services.gst.gov.in/commonservices/hsn/search/qsearch"
    RFN_URL: ClassVar[str] = "https://services.gst.gov.in/publicservices/api/verifyRfn"

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

    @classmethod
    def _pan_headers(cls) -> dict[str, str]:
        """As :meth:`_details_headers`, but referred from the PAN search page.

        Derived rather than copied: the portal fingerprints clients, so the
        two header sets must not drift apart.
        """
        return {**cls._details_headers(), "Referer": f"{cls.BASE_URL}{cls.PAN_SEARCH_PATH}"}

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
    def _pan_payload(pan: str, captcha: str) -> dict[str, str]:
        """Validate the PAN before a captcha is spent on it."""
        number = validate_pan(pan)
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        return {"panNO": number, "captcha": text}

    @staticmethod
    def _as_registrations(response: httpx.Response) -> tuple[Registration, ...]:
        """Read ``gstinResList``; its absence is how a rejection arrives.

        A PAN with no registrations answers with an empty list, so an empty
        result is a fact rather than a failure. A rejected request carries no
        list at all, the same way a rejected lookup carries no ``gstin``.
        """
        payload = _json_object(response)
        if "gstinResList" not in payload:
            raise _rejection(payload)
        entries = _entries(payload.get("gstinResList"))
        return tuple(Registration.from_payload(entry) for entry in entries)

    @staticmethod
    def _as_hsn_codes(response: httpx.Response) -> tuple[HSNCode, ...]:
        """``{"data": [{"c": ..., "n": ...}]}``; an unknown code is an empty list."""
        payload = _json_object(response)
        return tuple(HSNCode.from_payload(entry) for entry in _entries(payload.get("data")))

    @staticmethod
    def _as_composition(response: httpx.Response) -> tuple[CompositionTaxpayer, ...]:
        """The opted-in/out list, which arrives under ``data`` behind an envelope."""
        data = _envelope(response)
        rows: object = data
        if isinstance(data, dict):
            mapping = cast(dict[str, Any], data)
            rows = mapping.get("tpList") or mapping.get("list") or mapping.get("response")
        return tuple(CompositionTaxpayer.from_payload(entry) for entry in _entries(rows))

    @staticmethod
    def _as_application(response: httpx.Response) -> ApplicationStatus:
        """A tracked ARN. No ``arn`` in the body means the portal refused it."""
        payload = _json_object(response)
        body = as_mapping(payload.get("data")) or payload
        if not body.get("arn"):
            raise _rejection(payload)
        return ApplicationStatus.from_payload(body)

    @staticmethod
    def _as_reference(response: httpx.Response, reference: str) -> ReferenceNumber:
        """A verified RFN.

        An unrecognised reference is an answer, not a failure: the whole point
        is to learn that a notice did not come from the department. Only a
        portal-level rejection, such as a bad captcha, raises.
        """
        payload = _json_object(response)
        if payload.get("errorCode") and not payload.get("docType"):
            raise _rejection(payload)
        body = as_mapping(payload.get("data")) or payload
        return ReferenceNumber.from_payload({"refId": reference, **body})

    @staticmethod
    def _as_practitioners(response: httpx.Response) -> tuple[GSTPractitioner, ...]:
        """The directory answers with a bare list rather than an envelope."""
        try:
            body: object = response.json()
        except ValueError as error:
            raise TaxpayerLookupError("portal returned a non-JSON body") from error
        if isinstance(body, dict):
            mapping = cast(dict[str, Any], body)
            if mapping.get("errorCode"):
                raise _rejection(mapping)
            body = mapping.get("data")
        return tuple(GSTPractitioner.from_payload(entry) for entry in _entries(body))

    @staticmethod
    def _as_temporary(response: httpx.Response) -> TemporaryRegistration:
        payload = _json_object(response)
        body = as_mapping(payload.get("data")) or payload
        if not body.get("tempId"):
            raise _rejection(payload)
        return TemporaryRegistration.from_payload(body)

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
            raise TaxpayerLookupError(f"request to {self.DETAILS_PATH} failed: {error}") from error
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

    def fetch_registrations_by_pan(self, pan: str, captcha: str) -> tuple[Registration, ...]:
        """Every GSTIN registered under ``pan``, across all states.

        Costs one captcha, solved on this same instance, exactly as
        :meth:`fetch_details` does. The captcha is single-use: a second call
        needs a fresh one.
        """
        payload = self._pan_payload(pan, captcha)
        try:
            response = self._client.post(
                self.REGISTRATIONS_PATH, json=payload, headers=self._pan_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.REGISTRATIONS_PATH} failed: {error}"
            ) from error
        return self._as_registrations(response)

    def search_hsn_codes(self, text: str, *, by: str = "code") -> tuple[HSNCode, ...]:
        """Look a commodity or service code up by code or description.

        Needs no captcha and no session. ``by="code"`` matches leading digits,
        ``by="description"`` matches words. An unknown code answers with an
        empty tuple rather than an error.
        """
        selected = "byCode" if by == "code" else "byDesc"
        try:
            response = self._client.get(
                self.HSN_URL,
                params={"inputText": text, "selectedType": selected, "category": "null"},
                headers=self._api_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.HSN_URL} failed: {error}") from error
        return self._as_hsn_codes(response)

    def search_practitioners(
        self,
        *,
        state_code: str | None = None,
        pincode: str | None = None,
        name: str | None = None,
        enrolment_number: str | None = None,
    ) -> tuple[GSTPractitioner, ...]:
        """Find registered GST practitioners. Needs no captcha.

        **Returns personal data about named individuals.** Narrow the search:
        a bare state code returns everyone enrolled in that state, which is a
        directory dump rather than a lookup, and is not what the portal
        publishes this for. See :class:`~gst_validator.search.GSTPractitioner`.
        """
        payload: dict[str, Any] = {
            "searchType": "E" if enrolment_number else "A",
            "trpNam": name,
            "stCd": state_code,
            "dstCd": None,
            "pinCd": pincode or "",
        }
        if enrolment_number:
            payload["enrlNo"] = enrolment_number
        try:
            response = self._client.post(
                self.PRACTITIONER_PATH, json=payload, headers=self._api_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.PRACTITIONER_PATH} failed: {error}"
            ) from error
        return self._as_practitioners(response)

    def search_composition_taxpayers(
        self, state_code: str, financial_year: str, captcha: str, *, opted_in: bool = True
    ) -> tuple[CompositionTaxpayer, ...]:
        """Taxpayers who opted into or out of the composition scheme. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        payload = {
            "op": "O" if opted_in else "R",
            "captcha": text,
            "stcd": state_code,
            "fy": financial_year,
        }
        try:
            response = self._client.post(
                self.COMPOSITION_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.COMPOSITION_PATH} failed: {error}"
            ) from error
        return self._as_composition(response)

    def track_application(self, arn: str, captcha: str) -> ApplicationStatus:
        """Where an application has reached, by its ARN. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        try:
            response = self._client.get(
                self.ARN_PATH,
                params={"arn": arn.strip().upper(), "captcha": text},
                headers=self._api_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.ARN_PATH} failed: {error}") from error
        return self._as_application(response)

    def verify_reference_number(self, reference: str, captcha: str) -> ReferenceNumber:
        """Check whether a document reference number was issued by the department.

        One captcha. A reference the portal does not recognise comes back with
        ``is_genuine`` false, which is the answer, not an error.
        """
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        cleaned = reference.strip().upper()
        try:
            response = self._client.post(
                self.RFN_URL,
                json={"refId": cleaned, "captcha": text},
                headers=self._details_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.RFN_URL} failed: {error}") from error
        return self._as_reference(response, cleaned)

    def search_temporary_registration(
        self, temporary_id: str, captcha: str, *, mobile: str | None = None
    ) -> TemporaryRegistration:
        """Look a temporary registration up by its id. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        payload = {
            "tempId": temporary_id.strip().upper(),
            "stateCd": None,
            "mobNum": mobile,
            "captcha": text,
        }
        try:
            response = self._client.post(
                self.TEMPORARY_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.TEMPORARY_PATH} failed: {error}"
            ) from error
        return self._as_temporary(response)

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
            raise TaxpayerLookupError(f"request to {self.DETAILS_PATH} failed: {error}") from error
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

    async def fetch_registrations_by_pan(self, pan: str, captcha: str) -> tuple[Registration, ...]:
        """Every GSTIN registered under ``pan``, across all states.

        Costs one captcha, solved on this same instance. The captcha is
        single-use: a second call needs a fresh one.
        """
        payload = self._pan_payload(pan, captcha)
        try:
            response = await self._client.post(
                self.REGISTRATIONS_PATH, json=payload, headers=self._pan_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.REGISTRATIONS_PATH} failed: {error}"
            ) from error
        return self._as_registrations(response)

    async def search_hsn_codes(self, text: str, *, by: str = "code") -> tuple[HSNCode, ...]:
        """Look a commodity or service code up by code or description.

        Needs no captcha and no session. ``by="code"`` matches leading digits,
        ``by="description"`` matches words. An unknown code answers with an
        empty tuple rather than an error.
        """
        selected = "byCode" if by == "code" else "byDesc"
        try:
            response = await self._client.get(
                self.HSN_URL,
                params={"inputText": text, "selectedType": selected, "category": "null"},
                headers=self._api_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.HSN_URL} failed: {error}") from error
        return self._as_hsn_codes(response)

    async def search_practitioners(
        self,
        *,
        state_code: str | None = None,
        pincode: str | None = None,
        name: str | None = None,
        enrolment_number: str | None = None,
    ) -> tuple[GSTPractitioner, ...]:
        """Find registered GST practitioners. Needs no captcha.

        **Returns personal data about named individuals.** Narrow the search:
        a bare state code returns everyone enrolled in that state, which is a
        directory dump rather than a lookup, and is not what the portal
        publishes this for. See :class:`~gst_validator.search.GSTPractitioner`.
        """
        payload: dict[str, Any] = {
            "searchType": "E" if enrolment_number else "A",
            "trpNam": name,
            "stCd": state_code,
            "dstCd": None,
            "pinCd": pincode or "",
        }
        if enrolment_number:
            payload["enrlNo"] = enrolment_number
        try:
            response = await self._client.post(
                self.PRACTITIONER_PATH, json=payload, headers=self._api_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.PRACTITIONER_PATH} failed: {error}"
            ) from error
        return self._as_practitioners(response)

    async def search_composition_taxpayers(
        self, state_code: str, financial_year: str, captcha: str, *, opted_in: bool = True
    ) -> tuple[CompositionTaxpayer, ...]:
        """Taxpayers who opted into or out of the composition scheme. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        payload = {
            "op": "O" if opted_in else "R",
            "captcha": text,
            "stcd": state_code,
            "fy": financial_year,
        }
        try:
            response = await self._client.post(
                self.COMPOSITION_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.COMPOSITION_PATH} failed: {error}"
            ) from error
        return self._as_composition(response)

    async def track_application(self, arn: str, captcha: str) -> ApplicationStatus:
        """Where an application has reached, by its ARN. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        try:
            response = await self._client.get(
                self.ARN_PATH,
                params={"arn": arn.strip().upper(), "captcha": text},
                headers=self._api_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.ARN_PATH} failed: {error}") from error
        return self._as_application(response)

    async def verify_reference_number(self, reference: str, captcha: str) -> ReferenceNumber:
        """Check whether a document reference number was issued by the department.

        One captcha. A reference the portal does not recognise comes back with
        ``is_genuine`` false, which is the answer, not an error.
        """
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        cleaned = reference.strip().upper()
        try:
            response = await self._client.post(
                self.RFN_URL,
                json={"refId": cleaned, "captcha": text},
                headers=self._details_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(f"request to {self.RFN_URL} failed: {error}") from error
        return self._as_reference(response, cleaned)

    async def search_temporary_registration(
        self, temporary_id: str, captcha: str, *, mobile: str | None = None
    ) -> TemporaryRegistration:
        """Look a temporary registration up by its id. One captcha."""
        text = captcha.strip()
        if not text:
            raise TaxpayerLookupError("captcha text must not be empty")
        payload = {
            "tempId": temporary_id.strip().upper(),
            "stateCd": None,
            "mobNum": mobile,
            "captcha": text,
        }
        try:
            response = await self._client.post(
                self.TEMPORARY_PATH, json=payload, headers=self._details_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TaxpayerLookupError(
                f"request to {self.TEMPORARY_PATH} failed: {error}"
            ) from error
        return self._as_temporary(response)

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
