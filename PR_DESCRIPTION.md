## Problem

Issue #5: `LinkedInProvider.fetch_messages()` was unimplemented (raised `NotImplementedError`). Sync could not retrieve messages for threads; `POST /sync` with a real provider would fail when calling `fetch_messages(platform_thread_id, cursor, limit)`.

## Root Cause

The provider in `libs/providers/linkedin/provider.py` only defined the `fetch_messages` signature and docstring; the method body was `raise NotImplementedError`. The job runner and storage already supported cursor-based pagination and message insertion; the missing piece was the actual Voyager API call and response parsing.

## Solution

- **Provider (`libs/providers/linkedin/provider.py`):**
  - Added `_build_headers()` and `_proxy_url()` (same header/proxy/CSRF pattern as issue #4).
  - Added `_get_my_profile_id(client)` to resolve current user `publicIdentifier` from Voyager `/me` (cached).
  - Added `_parse_event_to_message(event, my_profile_id)` to map one Voyager event to `LinkedInMessage`, with direction `"out"` when sender equals current user, else `"in"`. Malformed events (missing `from`/`createdAt`/`entityUrn`) are skipped.
  - Implemented `fetch_messages()`: GET `.../voyager/api/messaging/conversations/{id}/events` with `keyVersion=LEGACY_INBOX`, `q=events`, `count=limit`, and optional `createdBefore=cursor`; parse `elements` or `events`; return messages in chronological order (oldest first) and `next_cursor` as oldest message’s `createdAt` ms when a full page is returned, else `None`.
  - JSESSIONID required for CSRF; raises `ValueError` with a clear message if missing.
- **Job runner (`libs/core/job_runner.py`):**
  - Added `time.sleep(1.5)` between pages within a thread (after processing a page when `next_cursor` is not `None`), per issue #5 and #7 rate-limit requirements.
- **Dependencies:** Added `httpx>=0.27` to main `dependencies` in `pyproject.toml` for the provider HTTP client.

Alternatives considered: putting the 1.5s delay inside the provider was rejected so that the provider stays a single-request-per-call abstraction; the job runner owns pagination and rate limiting between calls.

## Testing

- **test_fetch_messages_returns_empty_list_and_none_cursor_when_no_events** — empty thread → `([], None)` (regression).
- **test_fetch_messages_returns_messages_and_none_cursor_when_fewer_than_limit** — partial page → `next_cursor is None`.
- **test_fetch_messages_returns_next_cursor_when_exactly_limit_events** — full page → `next_cursor` = oldest `createdAt` ms.
- **test_fetch_messages_direction_out_when_sender_is_my_profile_id** — self-sent → `direction "out"`.
- **test_fetch_messages_direction_in_when_sender_is_other** — other user → `direction "in"`.
- **test_fetch_messages_passes_cursor_as_created_before** — cursor passed as `createdBefore` param.
- **test_fetch_messages_skips_malformed_events** — missing from/createdAt/entityUrn → event skipped.
- **test_fetch_messages_raises_when_jsessionid_missing** / **test_fetch_messages_raises_when_jsessionid_empty_string** — fail fast with clear error.
- **test_fetch_messages_uses_proxy_when_configured** — proxy URL passed to `httpx.Client(proxy=...)`.
- **test_fetch_messages_chronological_order_oldest_first** — messages sorted by `sent_at`.
- **test_fetch_messages_accepts_events_key_alternatively** — response may use `events` key instead of `elements`.
- **test_build_headers_includes_csrf_and_required_headers** — Voyager headers and CSRF present.
- **test_run_sync_sleeps_between_pages** — job_runner calls `time.sleep(1.5)` when paginating.

Verify: `uv run pytest tests/ -v --tb=short` and `uv run python scripts/integration_smoke.py`.

## Before / After

- **Before:** `provider.fetch_messages(...)` raised `NotImplementedError`; sync could not fetch messages for any thread.
- **After:** `fetch_messages` returns `(list[LinkedInMessage], next_cursor)` from the Voyager events endpoint with cursor-based pagination, correct direction, and chronological order; job_runner applies a 1.5s delay between pages.

## Edge Cases Handled

- Empty thread (0 events) → `([], None)`.
- Fewer than `limit` events → `next_cursor is None`.
- Exactly `limit` events → `next_cursor` = oldest message’s `createdAt` ms.
- Malformed event (missing from/createdAt/entityUrn) → event skipped, rest parsed.
- Missing or empty JSESSIONID → `ValueError` before any request.
- Proxy `None` → no proxy passed to httpx.
- Response uses `events` key instead of `elements` → both accepted.
- Messages returned in chronological order (oldest first) via sort by `sent_at`.

Closes #5
