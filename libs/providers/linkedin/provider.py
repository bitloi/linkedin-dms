from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

_VOYAGER_BASE = "https://www.linkedin.com/voyager/api"
_VOYAGER_TIMEOUT_S = 30.0
_PAGE_SIZE = 20
_MAX_PAGES = 50
_DELAY_BETWEEN_PAGES_S = 1.5
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 2.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class LinkedInThread:
    platform_thread_id: str
    title: Optional[str]
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class LinkedInMessage:
    platform_message_id: str
    direction: str  # "in" | "out"
    sender: Optional[str]
    text: Optional[str]
    sent_at: datetime
    raw: Optional[dict[str, Any]] = None

@dataclass(frozen=True)
class AuthCheckResult:
    ok: bool
    error: Optional[str] = None

def _build_included_index(included: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the included array by entityUrn for O(1) lookups."""
    return {item["entityUrn"]: item for item in included if "entityUrn" in item}


def _extract_title(
    element: dict[str, Any],
    included_index: dict[str, dict[str, Any]],
) -> Optional[str]:
    """Build a human-readable title from participant mini-profiles.

    Looks up each participant URN in the pre-built included index.
    Returns comma-separated full names, or None if no names resolved.
    """
    names: list[str] = []
    for p in element.get("participants", []):
        urn = (
            p.get("*com.linkedin.voyager.messaging.MessagingMember")
            or p.get("participantUrn")
            or p.get("entityUrn", "")
        )
        if not urn:
            continue
        profile = included_index.get(urn)
        if profile is None:
            continue
        full = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
        if full:
            names.append(full)
    return ", ".join(names) if names else None


class LinkedInProvider:
    """LinkedIn DM provider.

    This file is the main contribution point.

    Contributors can implement this using:
    - Playwright (recommended): login via cookies and drive LinkedIn messaging UI
    - HTTP scraping: call internal endpoints using cookies + CSRF headers

    IMPORTANT:
    - Do NOT log cookies or auth headers.
    - Do NOT implement CAPTCHA/2FA bypass.
    """

    def __init__(self, *, auth: AccountAuth, proxy: Optional[ProxyConfig] = None):
        self.auth = auth
        self.proxy = proxy
        self._client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        """Return a reusable httpx.Client (lazy-initialized)."""
        if self._client is None or self._client.is_closed:
            proxy = self.proxy.url if self.proxy and self.proxy.url.strip() else None
            self._client = httpx.Client(proxy=proxy, timeout=_VOYAGER_TIMEOUT_S)
        return self._client

    def close(self) -> None:
        """Close the underlying HTTP client. Safe to call multiple times."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            self._client = None

    def __enter__(self) -> LinkedInProvider:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _build_headers(self) -> dict[str, str]:
        """Build Voyager API headers. JSESSIONID must be set for CSRF."""
        if not self.auth.jsessionid or not self.auth.jsessionid.strip():
            raise ValueError("JSESSIONID cookie required for Voyager API (CSRF)")
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "x-li-track": (
                '{"clientVersion":"1.13.8953","osName":"web",'
                '"timezoneOffset":4,"deviceFormFactor":"DESKTOP"}'
            ),
            "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
            "csrf-token": self.auth.jsessionid,
        }

    def _build_cookies(self) -> dict[str, str]:
        """Build the cookie dict for requests."""
        cookies: dict[str, str] = {"li_at": self.auth.li_at}
        if self.auth.jsessionid:
            cookies["JSESSIONID"] = self.auth.jsessionid
        return cookies

    def _get_with_retry(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """GET with retry on transient errors (429, 5xx).

        Exponential backoff: 2 s, 4 s, 8 s.  Honours Retry-After on 429.
        Non-retryable status codes are returned immediately for the caller
        to handle via raise_for_status().
        """
        last_exc: Optional[httpx.HTTPStatusError] = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            resp = client.get(url, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUS_CODES:
                return resp
            last_exc = httpx.HTTPStatusError(
                str(resp.status_code), request=resp.request, response=resp,
            )
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                break
            delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        pass
            logger.debug(
                "list_threads: %d from LinkedIn, retry %d/%d in %.1fs",
                resp.status_code, attempt + 1, _RETRY_MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def list_threads(self) -> list[LinkedInThread]:
        """Fetch all DM conversation threads with pagination.

        Calls the Voyager conversations endpoint page by page.  Stops when
        LinkedIn returns fewer results than the page size, when the reported
        total is reached, or after _MAX_PAGES pages (safety cap).

        Rate limiting: sleeps _DELAY_BETWEEN_PAGES_S between pages.
        Retries: transient errors (429 / 5xx) are retried with backoff.

        Raises:
            ValueError: If JSESSIONID is missing.
            httpx.HTTPStatusError: On non-retryable HTTP errors or after
                exhausting retries on transient errors.
        """
        headers = self._build_headers()
        cookies = self._build_cookies()
        client = self._get_client()

        all_threads: list[LinkedInThread] = []
        seen_urns: set[str] = set()
        included_index: dict[str, dict[str, Any]] = {}
        start = 0

        for page_num in range(1, _MAX_PAGES + 1):
            resp = self._get_with_retry(
                client,
                f"{_VOYAGER_BASE}/messaging/conversations",
                params={
                    "keyVersion": "LEGACY_INBOX",
                    "q": "participants",
                    "start": start,
                    "count": _PAGE_SIZE,
                },
                headers=headers,
                cookies=cookies,
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                data = {}

            # Accumulate included entities across pages for title resolution.
            included_index.update(_build_included_index(data.get("included", [])))

            elements = data.get("elements", [])
            if not isinstance(elements, list):
                elements = []

            for elem in elements:
                entity_urn = elem.get("entityUrn", "")
                if not entity_urn or entity_urn in seen_urns:
                    continue
                seen_urns.add(entity_urn)
                title = _extract_title(elem, included_index)
                all_threads.append(LinkedInThread(
                    platform_thread_id=entity_urn,
                    title=title,
                    raw=elem,
                ))

            logger.debug(
                "list_threads: page %d fetched %d elements (%d threads total)",
                page_num, len(elements), len(all_threads),
            )

            # Pagination stop conditions.
            if len(elements) < _PAGE_SIZE:
                break
            paging_total = (data.get("paging") or {}).get("total")
            if paging_total is not None and start + len(elements) >= paging_total:
                break

            start += _PAGE_SIZE
            time.sleep(_DELAY_BETWEEN_PAGES_S)
        else:
            logger.warning(
                "list_threads: reached max page limit (%d); %d threads fetched",
                _MAX_PAGES, len(all_threads),
            )

        logger.info("list_threads: %d threads across %d pages", len(all_threads), page_num)
        return all_threads

    def fetch_messages(
        self,
        *,
        platform_thread_id: str,
        cursor: Optional[str],
        limit: int = 50,
    ) -> tuple[list[LinkedInMessage], Optional[str]]:
        """Fetch messages for a thread incrementally.

        Args:
          platform_thread_id: stable thread id
          cursor: opaque provider cursor (None = start)
          limit: max messages per call

        TODO (contributors):
        - Decide cursor semantics (e.g. newest timestamp, message id, pagination token)
        - Return messages in chronological order (oldest -> newest) if possible
        - Return next_cursor to continue, or None if fully synced
        """
        raise NotImplementedError

    def send_message(
        self,
        *,
        recipient: str,
        text: str,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Send a DM.

        Args:
          recipient: profile public id / URN / conversation id (define in implementation)
          text: message body
          idempotency_key: optional. If provided, use it to avoid duplicate sends on retries.

        Returns:
          platform_message_id (or provider generated id)

        TODO (contributors):
        - Implement send via UI automation or internal endpoint
        - Add retry/backoff outside provider or inside implementation
        """
        raise NotImplementedError

    def check_auth(self) -> AuthCheckResult:
        """Perform a lightweight auth sanity check.

        MVP behavior:
        - verify required cookie presence
        - optionally verify optional cookie format
        - placeholder for future lightweight LinkedIn request

        IMPORTANT:
        - do not leak cookie values in errors
        """
        if not self.auth.li_at or not self.auth.li_at.strip():
            return AuthCheckResult(ok=False, error="missing li_at cookie")

        # Optional light validation only; do not expose cookie values
        if self.auth.jsessionid is not None and not self.auth.jsessionid.strip():
            return AuthCheckResult(ok=False, error="invalid JSESSIONID cookie")

        # Placeholder success until real provider/network validation is implemented.
        return AuthCheckResult(ok=True, error=None)
