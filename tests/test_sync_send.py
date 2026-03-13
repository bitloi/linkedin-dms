"""Tests for sync/send orchestration (run_sync, run_send) and API behavior."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from libs.core.models import AccountAuth
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInMessage, LinkedInProvider, LinkedInThread


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.sqlite")


@pytest.fixture
def storage(db_path):
    s = Storage(db_path=db_path)
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def account_id(storage):
    auth = AccountAuth(li_at="test-li-at", jsessionid=None)
    return storage.create_account(label="test", auth=auth, proxy=None)


def test_run_sync_upserts_threads_and_messages(storage, account_id):
    thread = LinkedInThread(platform_thread_id="urn:li:conv:1", title="Alice", raw=None)
    msg = LinkedInMessage(
        platform_message_id="mid-1",
        direction="in",
        sender="alice",
        text="Hi",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = MagicMock(spec=LinkedInProvider)
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([msg], None)

    from apps.api.main import run_sync

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert result["synced_threads"] == 1
    assert result["synced_messages"] == 1
    provider.list_threads.assert_called_once()
    provider.fetch_messages.assert_called_once_with(
        platform_thread_id="urn:li:conv:1",
        cursor=None,
        limit=50,
    )
    threads = storage.list_threads(account_id=account_id)
    assert len(threads) == 1
    assert threads[0]["platform_thread_id"] == "urn:li:conv:1"
    assert threads[0]["title"] == "Alice"


def test_run_sync_uses_stored_cursor(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = MagicMock(spec=LinkedInProvider)
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([], None)
    thread_id = storage.upsert_thread(
        account_id=account_id, platform_thread_id="t1", title=None
    )
    storage.set_cursor(account_id=account_id, thread_id=thread_id, cursor="page2")

    from apps.api.main import run_sync

    run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=10,
    )
    provider.fetch_messages.assert_called_once_with(
        platform_thread_id="t1",
        cursor="page2",
        limit=10,
    )


def test_run_sync_normalizes_naive_sent_at(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    naive_dt = datetime(2025, 3, 1, 12, 0, 0)
    msg = LinkedInMessage(
        platform_message_id="m1",
        direction="out",
        sender=None,
        text="Bye",
        sent_at=naive_dt,
        raw=None,
    )
    provider = MagicMock(spec=LinkedInProvider)
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([msg], None)

    from apps.api.main import run_sync

    run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    rows = storage._conn.execute(
        "SELECT sent_at FROM messages WHERE account_id=? AND platform_message_id=?",
        (account_id, "m1"),
    ).fetchall()
    assert len(rows) == 1
    assert "Z" in rows[0]["sent_at"] or "+00:00" in rows[0]["sent_at"]


def test_run_send_returns_platform_message_id(storage, account_id):
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "plat-msg-123"

    from apps.api.main import run_send

    out = run_send(
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="key-1",
    )
    assert out == "plat-msg-123"
    provider.send_message.assert_called_once_with(
        recipient="bob",
        text="Hello",
        idempotency_key="key-1",
    )


def test_sync_endpoint_404_for_unknown_account(db_path):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    empty_storage = Storage(db_path=db_path)
    empty_storage.migrate()
    try:
        with patch("apps.api.main.storage", empty_storage):
            client = TestClient(app)
            resp = client.post("/sync", json={"account_id": 1, "limit_per_thread": 50})
        assert resp.status_code == 404
    finally:
        empty_storage.close()


def test_sync_endpoint_501_when_provider_not_implemented(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={"account_id": account_id, "limit_per_thread": 50},
        )
    assert resp.status_code == 501
    assert "not implemented" in resp.json()["detail"].lower()


def test_send_endpoint_404_for_unknown_account(db_path):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    empty_storage = Storage(db_path=db_path)
    empty_storage.migrate()
    try:
        with patch("apps.api.main.storage", empty_storage):
            client = TestClient(app)
            resp = client.post(
                "/send",
                json={
                    "account_id": 1,
                    "recipient": "x",
                    "text": "hi",
                    "idempotency_key": None,
                },
            )
        assert resp.status_code == 404
    finally:
        empty_storage.close()
