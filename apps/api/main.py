from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from libs.core.job_runner import run_send, run_sync, SyncResult
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

app = FastAPI(title="Desearch LinkedIn DMs", version="0.0.2")

storage = Storage()
storage.migrate()


class AccountCreateIn(BaseModel):
    label: str = Field(..., description="Human label, e.g. 'sales-1'")
    li_at: str = Field(..., description="LinkedIn li_at cookie value")
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    proxy_url: str | None = Field(None, description="Optional proxy URL")


class SendIn(BaseModel):
    account_id: int
    recipient: str = Field(..., min_length=1, description="Recipient id (profile URN or conversation id)")
    text: str = Field(..., min_length=1, max_length=8000, description="Message body")
    idempotency_key: str | None = None


class SyncIn(BaseModel):
    account_id: int
    limit_per_thread: int = Field(50, ge=1, le=500, description="Messages per page")
    max_pages_per_thread: int | None = Field(
        1,
        ge=1,
        le=100,
        description="Max pages per thread (1=MVP); omit or null to exhaust cursor",
    )


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
    """Trigger a sync. Default one page per thread (MVP); set max_pages_per_thread or null to exhaust."""
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    provider = LinkedInProvider(auth=auth, proxy=proxy)
    try:
        result: SyncResult = run_sync(
            account_id=body.account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=body.limit_per_thread,
            max_pages_per_thread=body.max_pages_per_thread,
        )
        return {
            "ok": True,
            "synced_threads": result.synced_threads,
            "messages_inserted": result.messages_inserted,
            "messages_skipped_duplicate": result.messages_skipped_duplicate,
            "pages_fetched": result.pages_fetched,
        }
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
