# Desearch LinkedIn DMs

LinkedIn DMs is a Python service for storing LinkedIn messaging data in SQLite and exposing it through a small FastAPI API and CLI.

The current repository is no longer just a skeleton. It already includes:
- account creation and cookie refresh endpoints
- SQLite migrations and persistence for accounts, threads, messages, cursors, and outbound sends
- a LinkedIn provider that can list threads, fetch messages, send messages, and perform a lightweight auth check
- log and error redaction for sensitive fields such as `li_at`, `JSESSIONID`, proxy URLs, and tokens

What is still true is that LinkedIn is a moving target. Some parts are implemented against private Voyager and GraphQL endpoints, so reliability depends on cookie validity, current LinkedIn query IDs, anti-bot responses, and optional Playwright cookie harvesting.

## Repository layout

```text
.
├─ apps/
│  ├─ api/                 # FastAPI application
│  └─ cli/                 # CLI entrypoint for sync/send without uvicorn
├─ libs/
│  ├─ core/                # models, storage, crypto, cookie parsing, redaction, job orchestration
│  └─ providers/
│     └─ linkedin/         # LinkedIn-specific HTTP + Playwright-assisted provider
├─ docs/
│  ├─ architecture.md
│  ├─ features.md
│  └─ known-issues.md
├─ scripts/
└─ tests/
```

## Requirements

- Python 3.11+
- SQLite, stored in `./desearch_linkedin_dms.sqlite` by default
- `li_at` cookie for every account
- `JSESSIONID` for Voyager/GraphQL endpoints used by thread listing and message fetch
- optional Playwright when LinkedIn or Cloudflare blocks cookie-only GraphQL access

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional browser support for Cloudflare cookie harvesting:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Optional at-rest encryption for stored auth and proxy payloads:

```bash
export DESEARCH_ENCRYPTION_KEY="$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
```

If `DESEARCH_ENCRYPTION_KEY` is not set, the app still works, but auth and proxy JSON are stored in plaintext and the process logs a one-time warning.

## Running the API

```bash
uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8899
```

Useful endpoints:
- `GET /health`
- `POST /accounts`
- `POST /accounts/refresh`
- `GET /auth/check?account_id=1`
- `GET /threads?account_id=1`
- `POST /sync`
- `POST /send`
- `GET /sends?account_id=1`

Swagger UI is available at <http://127.0.0.1:8899/docs>.

## Running the CLI

The CLI uses the same storage and provider stack as the API.

```bash
python -m apps.cli sync --account-id 1
python -m apps.cli send --account-id 1 --recipient 'urn:li:fsd_profile:123' --text 'Hello'
```

Useful sync options:
- `--db-path PATH`
- `--limit-per-thread N`
- `--max-pages-per-thread N`
- `--exhaust-pagination`
- `--delay-threads SEC`
- `--delay-pages SEC`

Useful send option:
- `--idempotency-key KEY`

## Account authentication input

`POST /accounts` and `POST /accounts/refresh` accept either:
- explicit `li_at` and optional `jsessionid`
- a `cookies` field containing either a raw cookie header string or a JSON cookie export

Examples:

```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -d '{"label":"sales-1","li_at":"REDACTED","jsessionid":"ajax:REDACTED"}'
```

```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -d '{"label":"sales-1","cookies":"li_at=REDACTED; JSESSIONID=ajax:REDACTED"}'
```

Refresh an existing account without recreating it:

```bash
curl -s -X POST http://127.0.0.1:8899/accounts/refresh \
  -H 'Content-Type: application/json' \
  -d '{"account_id":1,"cookies":"li_at=REDACTED; JSESSIONID=ajax:REDACTED"}'
```

Quick auth sanity check:

```bash
curl -s 'http://127.0.0.1:8899/auth/check?account_id=1'
```

## Sync behavior

`POST /sync` and `python -m apps.cli sync` both call `libs.core.job_runner.run_sync()`.

Current behavior:
- loads account auth and optional proxy from storage
- calls `LinkedInProvider.list_threads()`
- upserts each thread into SQLite
- fetches messages page by page with cursor support
- inserts only new messages, counting duplicate skips separately
- stores the latest cursor in `sync_cursors`
- sleeps between threads and pages to reduce rate-limit pressure
- returns summary counts including `rate_limited`

Default API sync payload:

```json
{
  "account_id": 1,
  "limit_per_thread": 50,
  "max_pages_per_thread": 1,
  "delay_between_threads_s": 2.0,
  "delay_between_pages_s": 1.5
}
```

Set `max_pages_per_thread` to `null` in the API or pass `--exhaust-pagination` in the CLI to keep following cursors until exhaustion.

## Send behavior

`POST /send` and `python -m apps.cli send` both call `libs.core.job_runner.run_send()`.

Current behavior:
- creates or reuses an outbound send record before calling LinkedIn
- enforces idempotency through the `outbound_sends` table when a key is provided
- retries transient network errors and backs off on rate limiting
- stores successful outbound messages in both `outbound_sends` and `messages`
- exposes historical send records through `GET /sends`

## Storage summary

The SQLite database currently contains these tables:
- `accounts`
- `threads`
- `messages`
- `sync_cursors`
- `schema_version`
- `outbound_sends`

Migrations also add message direction constraints and useful indexes.

## Security notes

The codebase already includes several concrete protections:
- `AccountAuth`, `ProxyConfig`, and `LinkedInProvider` redact their own string representations
- `configure_logging()` installs `SecretRedactingFilter` on the root logger
- `redact_string()` and `redact_for_log()` sanitize logs, dict payloads, and exception text
- API validation and `HTTPException` detail strings pass through redaction helpers before returning to clients
- optional Fernet encryption protects stored auth and proxy JSON at rest

Even with those safeguards:
- do not commit real cookies
- do not paste real cookies into issue trackers or logs
- treat `li_at`, `JSESSIONID`, proxy URLs, and any exported cookie bundle as secrets

## What to read next

- `docs/features.md` for implementation status by feature
- `docs/architecture.md` for component and request flow details
- `docs/known-issues.md` for sharp edges and operational caveats
