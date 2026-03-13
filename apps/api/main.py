from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from libs.core.models import AccountAuth, ProxyConfig
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInMessage, LinkedInProvider, LinkedInThread

app = FastAPI(title="Desearch LinkedIn DMs", version="0.0.2")

storage = Storage()
storage.migrate()


def _normalize_sent_at(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def run_sync(
    account_id: int,
    storage: Storage,
    provider: LinkedInProvider,
    limit_per_thread: int,
) -> dict:
    threads = provider.list_threads()
    synced_threads = 0
    synced_messages = 0
    for t in threads:
        thread_id = storage.upsert_thread(
            account_id=account_id,
            platform_thread_id=t.platform_thread_id,
            title=t.title,
        )
        cursor = storage.get_cursor(account_id=account_id, thread_id=thread_id)
        msgs, next_cursor = provider.fetch_messages(
            platform_thread_id=t.platform_thread_id,
            cursor=cursor,
            limit=limit_per_thread,
        )
        for m in msgs:
            storage.insert_message(
                account_id=account_id,
                thread_id=thread_id,
                platform_message_id=m.platform_message_id,
                direction=m.direction,
                sender=m.sender,
                text=m.text,
                sent_at=_normalize_sent_at(m.sent_at),
                raw=m.raw,
            )
            synced_messages += 1
        storage.set_cursor(account_id=account_id, thread_id=thread_id, cursor=next_cursor)
        synced_threads += 1
    return {"synced_threads": synced_threads, "synced_messages": synced_messages}


def run_send(
    storage: Storage,
    provider: LinkedInProvider,
    recipient: str,
    text: str,
    idempotency_key: str | None,
) -> str:
    return provider.send_message(
        recipient=recipient,
        text=text,
        idempotency_key=idempotency_key,
    )


class AccountCreateIn(BaseModel):
    label: str = Field(..., description="Human label, e.g. 'sales-1'")
    li_at: str = Field(..., description="LinkedIn li_at cookie value")
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    proxy_url: str | None = Field(None, description="Optional proxy URL")


class SendIn(BaseModel):
    account_id: int
    recipient: str
    text: str
    idempotency_key: str | None = None


class SyncIn(BaseModel):
    account_id: int
    limit_per_thread: int = 50


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/accounts")
def create_account(body: AccountCreateIn):
    auth = AccountAuth(li_at=body.li_at, jsessionid=body.jsessionid)
    proxy = ProxyConfig(url=body.proxy_url) if body.proxy_url else None
    account_id = storage.create_account(label=body.label, auth=auth, proxy=proxy)
    return {"account_id": account_id}


@app.get("/threads")
def list_threads(account_id: int):
    return {"threads": storage.list_threads(account_id=account_id)}


@app.post("/sync")
def sync_account(body: SyncIn):
    """Trigger a sync. One page per thread (MVP)."""
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    provider = LinkedInProvider(auth=auth, proxy=proxy)
    try:
        result = run_sync(
            account_id=body.account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=body.limit_per_thread,
        )
        return {"ok": True, **result}
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
        ) from None


@app.post("/send")
def send_message(body: SendIn):
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    provider = LinkedInProvider(auth=auth, proxy=proxy)
    try:
        platform_message_id = run_send(
            storage=storage,
            provider=provider,
            recipient=body.recipient,
            text=body.text,
            idempotency_key=body.idempotency_key,
        )
        return {"ok": True, "platform_message_id": platform_message_id}
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
        ) from None
