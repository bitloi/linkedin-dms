"""Tests for LinkedInProvider.fetch_messages (Voyager API events)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import LinkedInMessage, LinkedInProvider


def _me_response(public_identifier: str = "my-profile") -> dict:
    return {"publicIdentifier": public_identifier}


def _event(
    *,
    entity_urn: str = "urn:li:msg:1",
    created_at_ms: int = 1000000,
    public_identifier: str = "other-user",
    body: str | None = "Hello",
) -> dict:
    return {
        "entityUrn": entity_urn,
        "createdAt": created_at_ms,
        "from": {
            "member": {
                "miniProfile": {
                    "publicIdentifier": public_identifier,
                },
            },
        },
        "eventContent": {"body": body} if body is not None else {},
    }


@pytest.fixture
def auth():
    return AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")


@pytest.fixture
def provider(auth):
    return LinkedInProvider(auth=auth, proxy=None)


def test_fetch_messages_returns_empty_list_and_none_cursor_when_no_events(provider):
    """Regression: empty thread returns ([], None)."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="conv-1",
            cursor=None,
            limit=50,
        )
    assert messages == []
    assert next_cursor is None
    assert mock_client.get.call_count == 2


def test_fetch_messages_returns_messages_and_none_cursor_when_fewer_than_limit(provider):
    """Fewer than limit events → next_cursor is None."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=2000000, public_identifier="bob", body="Hi")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].platform_message_id == "urn:li:msg:1"
    assert messages[0].direction == "in"
    assert messages[0].sender == "bob"
    assert messages[0].text == "Hi"
    assert next_cursor is None


def test_fetch_messages_returns_next_cursor_when_exactly_limit_events(provider):
    """Exactly limit events → next_cursor is oldest message's createdAt ms."""
    oldest_ts = 1000000
    events = [
        _event(entity_urn="urn:li:msg:1", created_at_ms=oldest_ts, public_identifier="alice", body="First"),
        _event(entity_urn="urn:li:msg:2", created_at_ms=2000000, public_identifier="me", body="Second"),
    ]
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me")),
        _mock_resp({"elements": events}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=2,
        )
    assert len(messages) == 2
    assert next_cursor == str(oldest_ts)


def test_fetch_messages_direction_out_when_sender_is_my_profile_id(provider):
    """Event from current user → direction 'out'."""
    ev = _event(public_identifier="me-user", body="Sent by me")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me-user")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].direction == "out"
    assert messages[0].sender == "me-user"


def test_fetch_messages_direction_in_when_sender_is_other(provider):
    """Event from other user → direction 'in'."""
    ev = _event(public_identifier="other-user", body="From them")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("my-profile")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].direction == "in"
    assert messages[0].sender == "other-user"


def test_fetch_messages_passes_cursor_as_created_before(provider):
    """Cursor is sent as createdBefore query param."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client):
        provider.fetch_messages(
            platform_thread_id="conv-1",
            cursor="999000",
            limit=50,
        )
    events_call = mock_client.get.call_args_list[1]
    assert events_call.kwargs.get("params", {}).get("createdBefore") == "999000"


def test_fetch_messages_skips_malformed_events(provider):
    """Malformed events (missing from/createdAt/entityUrn) are skipped."""
    good = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="x", body="Ok")
    bad_no_from = {"entityUrn": "urn:li:msg:2", "createdAt": 2000}
    bad_no_created = {"entityUrn": "urn:li:msg:3", "from": {"member": {"miniProfile": {"publicIdentifier": "y"}}}}
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [good, bad_no_from, bad_no_created]}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].platform_message_id == "urn:li:msg:1"
    assert next_cursor is None


def test_fetch_messages_raises_when_jsessionid_missing():
    """Missing JSESSIONID raises ValueError before any request."""
    auth = AccountAuth(li_at="li", jsessionid=None)
    provider = LinkedInProvider(auth=auth, proxy=None)
    with pytest.raises(ValueError, match="JSESSIONID cookie required"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_raises_when_jsessionid_empty_string():
    """Empty string JSESSIONID raises ValueError."""
    auth = AccountAuth(li_at="li", jsessionid="   ")
    provider = LinkedInProvider(auth=auth, proxy=None)
    with pytest.raises(ValueError, match="JSESSIONID cookie required"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_uses_proxy_when_configured(auth):
    """Provider passes proxy URL to httpx.Client when proxy is set."""
    proxy_config = ProxyConfig(url="http://proxy:8080")
    provider = LinkedInProvider(auth=auth, proxy=proxy_config)
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client) as mock_httpx_client:
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
    mock_httpx_client.assert_called_once()
    call_kw = mock_httpx_client.call_args[1]
    assert call_kw.get("proxy") == "http://proxy:8080"


def test_fetch_messages_chronological_order_oldest_first(provider):
    """Messages are returned oldest first (API often returns newest first)."""
    ev1 = _event(entity_urn="urn:li:msg:1", created_at_ms=3000000, public_identifier="a", body="Third")
    ev2 = _event(entity_urn="urn:li:msg:2", created_at_ms=1000000, public_identifier="b", body="First")
    ev3 = _event(entity_urn="urn:li:msg:3", created_at_ms=2000000, public_identifier="c", body="Second")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [ev1, ev2, ev3]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert [m.text for m in messages] == ["First", "Second", "Third"]
    assert [m.platform_message_id for m in messages] == ["urn:li:msg:2", "urn:li:msg:3", "urn:li:msg:1"]


def test_fetch_messages_accepts_events_key_alternatively(provider):
    """Response may use 'events' key instead of 'elements'."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="u", body="Hi")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"events": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].text == "Hi"


def test_build_headers_includes_csrf_and_required_headers(provider):
    """_build_headers includes csrf-token and standard Voyager headers."""
    headers = provider._build_headers()
    assert headers.get("csrf-token") == "ajax:csrf123"
    assert "User-Agent" in headers
    assert headers.get("Accept") == "application/vnd.linkedin.normalized+json+2.1"
    assert headers.get("x-restli-protocol-version") == "2.0.0"


def test_run_sync_sleeps_between_pages():
    """job_runner sleeps 1.5s before fetching next page (rate limit)."""
    from unittest.mock import patch as mock_patch
    from libs.core.job_runner import run_sync
    from libs.core.storage import Storage
    from libs.providers.linkedin.provider import LinkedInThread

    storage = Storage(db_path=":memory:")
    storage.migrate()
    auth = AccountAuth(li_at="x", jsessionid="y")
    account_id = storage.create_account(label="a", auth=auth, proxy=None)
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = MagicMock()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.side_effect = [
        ([], "cursor2"),
        ([], None),
    ]
    with mock_patch("libs.core.job_runner.time.sleep") as mock_sleep:
        run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=50,
            max_pages_per_thread=None,
        )
    mock_sleep.assert_called_once_with(1.5)
    storage.close()


def _mock_resp(json_data: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = json_data
    return r


def _patch_client(mock_client: MagicMock):
    """Patch httpx.Client so context manager returns mock_client."""
    return patch("libs.providers.linkedin.provider.httpx.Client", return_value=MagicMock(
        __enter__=MagicMock(return_value=mock_client),
        __exit__=MagicMock(return_value=False),
    ))
