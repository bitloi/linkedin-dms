#!/usr/bin/env python3
"""Real integration check: temp DB, migrate, full CRUD, then API via TestClient."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

# Plaintext storage for this script
os.environ.pop("DESEARCH_ENCRYPTION_KEY", None)
if "DESEARCH_DB_PATH" in os.environ:
    del os.environ["DESEARCH_DB_PATH"]

from datetime import datetime, timezone

from libs.core.models import AccountAuth
from libs.core.storage import Storage


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "real_check.sqlite"
        storage = Storage(db_path=db_path)

        # 1. Migrate
        storage.migrate()

        # 2. Verify schema_version and indexes
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "schema_version should have one row"
        version = row[0]
        assert version >= 2, f"expected schema version >= 2, got {version}"

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%' ORDER BY name"
        )
        index_names = [r[0] for r in cur.fetchall()]
        assert "idx_threads_account_id" in index_names
        assert "idx_messages_thread_id" in index_names
        assert "idx_messages_account_id" in index_names
        conn.close()

        # 3. Full CRUD
        aid = storage.create_account(label="real-test", auth=AccountAuth(li_at="fake_cookie"), proxy=None)
        assert aid >= 1
        auth = storage.get_account_auth(aid)
        assert auth.li_at == "fake_cookie"

        tid = storage.upsert_thread(account_id=aid, platform_thread_id="conv_123", title="Test thread")
        assert tid >= 1
        tid2 = storage.upsert_thread(account_id=aid, platform_thread_id="conv_123", title="Updated title")
        assert tid2 == tid

        threads = storage.list_threads(account_id=aid)
        assert len(threads) == 1
        assert threads[0]["title"] == "Updated title"

        assert storage.get_cursor(account_id=aid, thread_id=tid) is None
        storage.set_cursor(account_id=aid, thread_id=tid, cursor="page_abc")
        assert storage.get_cursor(account_id=aid, thread_id=tid) == "page_abc"

        ts = datetime(2025, 3, 13, 10, 0, 0, tzinfo=timezone.utc)
        ok1 = storage.insert_message(
            account_id=aid, thread_id=tid, platform_message_id="msg_1", direction="in",
            sender="u2", text="Hello", sent_at=ts, raw=None,
        )
        assert ok1 is True
        ok2 = storage.insert_message(
            account_id=aid, thread_id=tid, platform_message_id="msg_1", direction="in",
            sender="u2", text="Hello", sent_at=ts, raw=None,
        )
        assert ok2 is False  # duplicate

        # 4. Idempotent migrate
        storage.migrate()
        assert storage._get_schema_version() == version

        storage.close()

    # 5. API smoke with TestClient (patch app storage to use temp DB)
    with tempfile.TemporaryDirectory() as tmp2:
        db_path2 = Path(tmp2) / "api_check.sqlite"
        storage2 = Storage(db_path=db_path2)
        storage2.migrate()

        import apps.api.main as api_mod
        from fastapi.testclient import TestClient

        orig_storage = api_mod.storage
        api_mod.storage = storage2
        try:
            client = TestClient(api_mod.app)
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json() == {"ok": True}

            r = client.post("/accounts", json={"label": "api-test", "li_at": "AQEDAWx0Y29va2llXXX"})
            assert r.status_code == 200
            data = r.json()
            assert "account_id" in data

            r = client.get("/threads", params={"account_id": data["account_id"]})
            assert r.status_code == 200
            assert r.json() == {"threads": []}
        finally:
            api_mod.storage = orig_storage
            storage2.close()

    print("OK: real storage + API check passed.")


if __name__ == "__main__":
    main()
