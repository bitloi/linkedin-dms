"""Tests for LinkedInProvider.list_threads() — Voyager API thread discovery."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    LinkedInProvider,
    LinkedInThread,
    _build_included_index,
    _extract_title,
    _PAGE_SIZE,
    _MAX_PAGES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def auth():
    return AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")


@pytest.fixture
def provider(auth):
    return LinkedInProvider(auth=auth, proxy=None)


def _voyager_response(
    elements: list[dict],
    *,
    total: int | None = None,
    start: int = 0,
    included: list[dict] | None = None,
) -> dict:
    """Build a realistic Voyager conversations response."""
    body: dict = {
        "elements": elements,
        "paging": {"start": start, "count": _PAGE_SIZE},
    }
    if total is not None:
        body["paging"]["total"] = total
    if included is not None:
        body["included"] = included
    return body


def _make_element(urn: str, participants: list[dict] | None = None) -> dict:
    elem: dict = {"entityUrn": urn}
    if participants is not None:
        elem["participants"] = participants
    return elem


def _mock_resp(data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.content = b"ok"
    r.raise_for_status = MagicMock()
    r.json.return_value = data
    return r


def _patch_client(mock_client: MagicMock):
    """Patch httpx.Client so _get_client() returns mock_client."""
    return patch("libs.providers.linkedin.provider.httpx.Client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Unit tests: _build_included_index
# ---------------------------------------------------------------------------

class TestBuildIncludedIndex:
    def test_indexes_by_entity_urn(self):
        included = [
            {"entityUrn": "urn:li:fs_miniProfile:alice", "firstName": "Alice"},
            {"entityUrn": "urn:li:fs_miniProfile:bob", "firstName": "Bob"},
        ]
        idx = _build_included_index(included)
        assert idx["urn:li:fs_miniProfile:alice"]["firstName"] == "Alice"
        assert idx["urn:li:fs_miniProfile:bob"]["firstName"] == "Bob"

    def test_skips_items_without_entity_urn(self):
        included = [{"firstName": "Ghost"}, {"entityUrn": "urn:li:x", "firstName": "OK"}]
        idx = _build_included_index(included)
        assert len(idx) == 1
        assert "urn:li:x" in idx

    def test_empty_list(self):
        assert _build_included_index([]) == {}


# ---------------------------------------------------------------------------
# Unit tests: _extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_title_from_participant_urn(self):
        idx = {"urn:a": {"entityUrn": "urn:a", "firstName": "Alice", "lastName": "Smith"}}
        elem = _make_element("urn:conv:1", [{"participantUrn": "urn:a"}])
        assert _extract_title(elem, idx) == "Alice Smith"

    def test_title_from_messaging_member_key(self):
        idx = {"urn:a": {"entityUrn": "urn:a", "firstName": "Alice", "lastName": "W"}}
        elem = _make_element("urn:conv:1", [
            {"*com.linkedin.voyager.messaging.MessagingMember": "urn:a"},
        ])
        assert _extract_title(elem, idx) == "Alice W"

    def test_title_from_entity_urn_fallback(self):
        idx = {"urn:b": {"entityUrn": "urn:b", "firstName": "Bob", "lastName": ""}}
        elem = _make_element("urn:conv:1", [{"entityUrn": "urn:b"}])
        assert _extract_title(elem, idx) == "Bob"

    def test_multiple_participants(self):
        idx = {
            "urn:a": {"entityUrn": "urn:a", "firstName": "Alice", "lastName": "A"},
            "urn:b": {"entityUrn": "urn:b", "firstName": "Bob", "lastName": "B"},
        }
        elem = _make_element("urn:conv:1", [
            {"participantUrn": "urn:a"},
            {"participantUrn": "urn:b"},
        ])
        assert _extract_title(elem, idx) == "Alice A, Bob B"

    def test_none_when_no_participants(self):
        elem = _make_element("urn:conv:1")
        assert _extract_title(elem, {}) is None

    def test_none_when_participant_not_in_index(self):
        elem = _make_element("urn:conv:1", [{"participantUrn": "urn:unknown"}])
        assert _extract_title(elem, {}) is None

    def test_none_when_name_fields_empty(self):
        idx = {"urn:a": {"entityUrn": "urn:a", "firstName": "", "lastName": ""}}
        elem = _make_element("urn:conv:1", [{"participantUrn": "urn:a"}])
        assert _extract_title(elem, idx) is None


# ---------------------------------------------------------------------------
# Integration: list_threads with mocked HTTP
# ---------------------------------------------------------------------------

class TestListThreads:
    def test_single_page_partial(self, provider):
        """Fewer than PAGE_SIZE elements → single page, no second request."""
        elems = [_make_element(f"urn:conv:{i}") for i in range(3)]
        data = _voyager_response(elems, total=3)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == 3
        assert all(isinstance(t, LinkedInThread) for t in threads)
        mock_client.get.assert_called_once()

    def test_empty_inbox(self, provider):
        data = _voyager_response([], total=0)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []

    def test_pagination_two_pages(self, provider):
        """Full first page triggers second page fetch."""
        page1 = [_make_element(f"urn:conv:{i}") for i in range(_PAGE_SIZE)]
        page2 = [_make_element(f"urn:conv:{_PAGE_SIZE + i}") for i in range(5)]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_voyager_response(page1, total=_PAGE_SIZE + 5)),
            _mock_resp(_voyager_response(page2, total=_PAGE_SIZE + 5, start=_PAGE_SIZE)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert len(threads) == _PAGE_SIZE + 5
        assert mock_client.get.call_count == 2
        # Verify start param on second call
        second_params = mock_client.get.call_args_list[1].kwargs["params"]
        assert second_params["start"] == _PAGE_SIZE

    def test_pagination_stops_at_total(self, provider):
        """Stops when start + returned >= total even if full page returned."""
        elems = [_make_element(f"urn:conv:{i}") for i in range(_PAGE_SIZE)]
        data = _voyager_response(elems, total=_PAGE_SIZE)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == _PAGE_SIZE
        mock_client.get.assert_called_once()

    def test_max_pages_safety_limit(self, provider):
        """Stops after _MAX_PAGES even if more data exists."""
        call_count = {"n": 0}
        big_total = _MAX_PAGES * _PAGE_SIZE + 100

        def _make_page_resp(*args, **kwargs):
            page = call_count["n"]
            call_count["n"] += 1
            elems = [_make_element(f"urn:conv:p{page}_{i}") for i in range(_PAGE_SIZE)]
            return _mock_resp(_voyager_response(elems, total=big_total, start=page * _PAGE_SIZE))

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = _make_page_resp
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert mock_client.get.call_count == _MAX_PAGES
        assert len(threads) == _MAX_PAGES * _PAGE_SIZE

    def test_deduplicates_across_pages(self, provider):
        """Same entityUrn on two pages → returned only once."""
        dup_elem = _make_element("urn:conv:dup")
        page1 = [dup_elem] + [_make_element(f"urn:conv:{i}") for i in range(_PAGE_SIZE - 1)]
        page2 = [dup_elem, _make_element("urn:conv:new")]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_voyager_response(page1, total=_PAGE_SIZE + 2)),
            _mock_resp(_voyager_response(page2, start=_PAGE_SIZE)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        urns = [t.platform_thread_id for t in threads]
        assert urns.count("urn:conv:dup") == 1
        assert "urn:conv:new" in urns

    def test_skips_elements_without_entity_urn(self, provider):
        elems = [{"someField": "value"}, _make_element("urn:conv:good")]
        data = _voyager_response(elems, total=2)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:conv:good"

    def test_accumulates_included_across_pages(self, provider):
        """Participant from page 1 included resolves title on page 2 element."""
        urn_alice = "urn:li:fs_miniProfile:alice"
        page1_included = [{"entityUrn": urn_alice, "firstName": "Alice", "lastName": "S"}]
        page1_elems = [_make_element(f"urn:conv:{i}") for i in range(_PAGE_SIZE)]
        page2_elem = _make_element("urn:conv:cross", [{"participantUrn": urn_alice}])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_voyager_response(page1_elems, total=_PAGE_SIZE + 1, included=page1_included)),
            _mock_resp(_voyager_response([page2_elem], start=_PAGE_SIZE)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        cross_thread = [t for t in threads if t.platform_thread_id == "urn:conv:cross"][0]
        assert cross_thread.title == "Alice S"

    def test_title_resolved_from_included(self, provider):
        urn = "urn:li:fs_miniProfile:alice"
        elem = _make_element("urn:conv:1", [{"participantUrn": urn}])
        included = [{"entityUrn": urn, "firstName": "Alice", "lastName": "Smith"}]
        data = _voyager_response([elem], total=1, included=included)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads[0].title == "Alice Smith"

    def test_http_error_propagates(self, provider):
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.content = b"forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_403
        with _patch_client(mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

    def test_requires_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.list_threads()

    def test_requires_jsessionid_not_blank(self):
        auth = AccountAuth(li_at="li", jsessionid="   ")
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.list_threads()

    def test_uses_proxy(self, auth):
        proxy = ProxyConfig(url="http://proxy:8080")
        p = LinkedInProvider(auth=auth, proxy=proxy)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(_voyager_response([], total=0))
        with _patch_client(mock_client) as mock_cls:
            p.list_threads()
        mock_cls.assert_called_once()
        assert mock_cls.call_args.kwargs.get("proxy") == "http://proxy:8080"

    def test_cookies_not_leaked_into_headers(self, provider):
        data = _voyager_response([], total=0)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.list_threads()
        call_kwargs = mock_client.get.call_args.kwargs
        headers_str = str(call_kwargs["headers"])
        assert "test-li-at" not in headers_str
        assert call_kwargs["cookies"]["li_at"] == "test-li-at"

    def test_sleeps_between_pages(self, provider):
        page1 = [_make_element(f"urn:conv:{i}") for i in range(_PAGE_SIZE)]
        page2 = [_make_element("urn:conv:last")]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_voyager_response(page1, total=_PAGE_SIZE + 1)),
            _mock_resp(_voyager_response(page2, start=_PAGE_SIZE)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        from libs.providers.linkedin.provider import _DELAY_BETWEEN_PAGES_S
        mock_sleep.assert_called_once_with(_DELAY_BETWEEN_PAGES_S)

    def test_no_sleep_on_single_page(self, provider):
        data = _voyager_response([_make_element("urn:conv:1")], total=1)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        mock_sleep.assert_not_called()

    def test_retries_on_429_then_succeeds(self, provider):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.request = MagicMock()
        data = _voyager_response([_make_element("urn:conv:1")], total=1)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [resp_429, _mock_resp(data)]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert len(threads) == 1

    def test_exhausts_retries_on_503(self, provider):
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.headers = {}
        resp_503.request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_503
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

    def test_retry_honours_retry_after_header(self, provider):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "10"}
        resp_429.request = MagicMock()
        data = _voyager_response([], total=0)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [resp_429, _mock_resp(data)]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        assert mock_sleep.call_args[0][0] >= 10.0

    def test_no_retry_on_403(self, provider):
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.content = b"forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_403
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()
        mock_sleep.assert_not_called()

    def test_client_reused_across_calls(self, provider):
        data = _voyager_response([], total=0)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client) as mock_cls:
            provider.list_threads()
            provider.list_threads()
        mock_cls.assert_called_once()

    def test_context_manager_closes_client(self, auth):
        mock_client = MagicMock()
        mock_client.is_closed = False
        with _patch_client(mock_client):
            p = LinkedInProvider(auth=auth)
            with p:
                p._get_client()
            mock_client.close.assert_called_once()

    def test_handles_non_dict_response(self, provider):
        """Non-dict JSON response treated as empty."""
        r = MagicMock()
        r.status_code = 200
        r.content = b"[]"
        r.raise_for_status = MagicMock()
        r.json.return_value = []
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []

    def test_handles_empty_response_body(self, provider):
        """Empty response body treated as empty."""
        r = MagicMock()
        r.status_code = 200
        r.content = b""
        r.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []


# ---------------------------------------------------------------------------
# Header / cookie construction
# ---------------------------------------------------------------------------

class TestBuildHeaders:
    def test_includes_csrf_and_required_headers(self, provider):
        headers = provider._build_headers()
        assert headers["csrf-token"] == "ajax:csrf123"
        assert "User-Agent" in headers
        assert headers["Accept"] == "application/vnd.linkedin.normalized+json+2.1"
        assert headers["x-restli-protocol-version"] == "2.0.0"
        assert "x-li-track" in headers
        assert "x-li-page-instance" in headers

    def test_build_cookies_includes_li_at_and_jsessionid(self, provider):
        cookies = provider._build_cookies()
        assert cookies["li_at"] == "test-li-at"
        assert cookies["JSESSIONID"] == "ajax:csrf123"

    def test_build_cookies_without_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        cookies = p._build_cookies()
        assert "li_at" in cookies
        assert "JSESSIONID" not in cookies
