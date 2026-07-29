# Copyright (c) 2026, Hypedrive
# License: MIT
"""
Thin, cached HTTP client for Chatwoot's REST API.

Design notes (see chatwoot_api_reference.md research doc for the live-verified
source of every shape/quirk referenced below):

- Auth header is `api_access_token`, NOT `Authorization: Bearer`.
- Use the User Access Token (Chatwoot Settings.api_token) — bot and platform
  tokens are rejected by conversation-read endpoints (confirmed via live 401s).
- Response envelopes are INCONSISTENT across endpoints:
    * GET /conversations           -> {"data": {"payload": [...]}}
    * GET /conversations/{id}      -> bare object (no wrapper)
    * GET /conversations/{id}/messages -> {"meta": {...}, "payload": [...]}
    * GET /contacts/{id}/conversations -> {"payload": [...]}  (no "data" key)
  Each accessor below unwraps its own endpoint's real shape rather than
  assuming one envelope convention project-wide.
- No rate-limit headers are exposed by this Chatwoot instance. We self-impose
  a short TTL cache (Chatwoot Settings.cache_ttl_seconds, default 20s) using
  frappe.cache() (redis) so concurrent Desk/CRM tabs don't each trigger a
  fresh upstream call.
- Message pagination supports BOTH directions:
    * `before=<message id>` — walk backward (older history, scroll-up UI).
    * `after=<message id>`  — walk forward/ascending (incremental polling).
  Verified against Chatwoot's own `message_finder.rb`: both params are
  supported server-side, and each page is hard-capped at 100 rows with NO
  truncation signal (no `total`/`has_more`/`next_cursor` anywhere in the
  response). A single page coming back with exactly 100 rows is therefore
  AMBIGUOUS — it could be the entire delta, or page 1 of many. Naively taking
  one page and advancing the cursor to its max id causes silent, permanent
  message loss on any burst > 100 messages (bulk import, reconnect after
  downtime, a rapid automation). `list_messages_incremental()` below
  implements the fix: a bounded drain loop that keeps re-fetching with the
  cursor advanced to the max id seen until a page comes back short (<100),
  capped at MAX_DRAIN_PAGES pages and DRAIN_WALL_CLOCK_SECONDS wall-clock so
  a pathological backlog can't run away. An unfinished drain safely resumes
  on the next poll cycle since the cursor only ever moves forward over
  confirmed ids.
- Private/internal Chatwoot notes (agent-only, never sent to the customer)
  must never reach a CRM/FE consumer — this is a confidentiality invariant,
  not a preference. Two independent filters are applied:
    1. `filter_internal_messages=true` query param on the messages endpoint.
    2. An independent client-side drop of any message where `private` is
       true, applied AFTER the query param, regardless of whether the
       param appears to have worked — some self-hosted Chatwoot builds are
       known to silently ignore this query param, so the query param alone
       is not sufficient.
"""

import json
import time

import frappe
import requests

CACHE_PREFIX = "frappe_chatwoot:v1"
DEFAULT_TTL = 20
HTTP_TIMEOUT = 15

# Chatwoot's message_finder.rb hard-caps each page at 100 rows.
CHATWOOT_PAGE_SIZE = 100
# Bounded drain-loop guards (see module docstring) — keep well under the
# scheduler/whitelisted-call's own execution ceiling.
MAX_DRAIN_PAGES = 5
DRAIN_WALL_CLOCK_SECONDS = 10


class ChatwootNotConfigured(frappe.ValidationError):
    pass


class ChatwootAPIError(frappe.ValidationError):
    pass


def _settings():
    if not frappe.db.exists("DocType", "Chatwoot Settings"):
        raise ChatwootNotConfigured("Chatwoot Settings doctype not found")
    settings = frappe.get_single("Chatwoot Settings")
    if not settings.enabled:
        raise ChatwootNotConfigured("Chatwoot integration is disabled")
    if not settings.base_url or not settings.account_id:
        raise ChatwootNotConfigured("Chatwoot Settings is missing base_url/account_id")
    return settings


def _api_token(settings=None) -> str:
    settings = settings or _settings()
    token = settings.get_password("api_token", raise_exception=False)
    if not token:
        raise ChatwootNotConfigured("Chatwoot Settings has no API token configured")
    return token


def _cache_ttl(settings=None) -> int:
    settings = settings or _settings()
    return int(settings.cache_ttl_seconds or DEFAULT_TTL)


def _cache_key(*parts) -> str:
    return ":".join([CACHE_PREFIX, *[str(p) for p in parts]])


def clear_cache():
    """Drop every frappe_chatwoot cache key. Called on Settings save and
    exposed as a whitelisted admin action for manual invalidation."""
    cache = frappe.cache()
    # frappe.cache() is a thin redis wrapper; delete_keys supports a glob
    # pattern on the redis backend used in production. Fall back to a no-op
    # if the cache backend doesn't support pattern deletes (e.g. in tests).
    try:
        cache.delete_keys(CACHE_PREFIX + "*")
    except Exception:
        frappe.log_error(title="frappe_chatwoot: cache clear fallback")


# ---------------------------------------------------------------------------
# Chatwoot Log — audit trail (see doctype/chatwoot_log). Best-effort: a
# logging failure must never mask or replace the real error being reported.
# ---------------------------------------------------------------------------


def _write_log(*, request_type: str, endpoint: str = "", status_code: int | None = None,
                conversation_id=None, payload=None, error: str = ""):
    if not frappe.db.exists("DocType", "Chatwoot Log"):
        return
    try:
        doc = frappe.new_doc("Chatwoot Log")
        doc.request_type = request_type
        doc.endpoint = endpoint
        if status_code is not None:
            doc.status_code = status_code
        if conversation_id is not None:
            doc.conversation_id = str(conversation_id)
        if payload is not None:
            try:
                doc.payload = json.dumps(payload)[:100000]
            except (TypeError, ValueError):
                doc.payload = json.dumps({"unserializable": str(payload)[:2000]})
        if error:
            doc.error = error[:2000]
        doc.insert(ignore_permissions=True)
    except Exception:
        # Logging must never break the caller's real error path.
        frappe.log_error(title="frappe_chatwoot: Chatwoot Log write failed")


def _extract_error_detail(resp) -> str:
    """Parse Chatwoot's JSON error envelope and return its real message.

    Chatwoot's validation/error responses commonly look like one of:
      {"message": "..."}
      {"error": "..."}
      {"errors": ["...", "..."]}
      {"errors": {"field": ["..."]}}
    Falls back to a clipped raw body (e.g. an HTML gateway error page) if the
    response isn't JSON or doesn't match a known shape.
    """
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300]

    if not isinstance(body, dict):
        return (resp.text or "")[:300]

    if body.get("message"):
        return str(body["message"])[:300]
    if body.get("error"):
        return str(body["error"])[:300]

    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        return ", ".join(str(e) for e in errors)[:300]
    if isinstance(errors, dict) and errors:
        parts = []
        for field, msgs in errors.items():
            msgs_str = ", ".join(str(m) for m in msgs) if isinstance(msgs, list) else str(msgs)
            parts.append(f"{field}: {msgs_str}")
        return "; ".join(parts)[:300]

    return (resp.text or "")[:300]


def _get(path: str, params: dict | None = None, *, cache_seconds: int | None = None):
    """GET against the Chatwoot account API, with short-TTL caching.

    `path` is relative to /api/v1/accounts/{account_id}, e.g. "/conversations".
    """
    settings = _settings()
    token = _api_token(settings)
    ttl = _cache_ttl(settings) if cache_seconds is None else cache_seconds

    cache_key = _cache_key("get", path, json.dumps(params or {}, sort_keys=True))
    if ttl > 0:
        cached = frappe.cache().get_value(cache_key)
        if cached is not None:
            try:
                return json.loads(cached)
            except (TypeError, ValueError):
                # A cache entry we can't decode (corrupt, truncated, or written
                # by an older key format) must not take the caller down with a
                # raw JSONDecodeError — drop it and fall through to a live read.
                frappe.cache().delete_value(cache_key)

    url = f"{settings.base_url}/api/v1/accounts/{settings.account_id}{path}"
    try:
        resp = requests.get(
            url,
            headers={"api_access_token": token},
            params=params or {},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        frappe.log_error(title="frappe_chatwoot: upstream request failed", message=str(e))
        _write_log(request_type="API Error", endpoint=path, error=str(e)[:2000])
        raise ChatwootAPIError(f"Could not reach Chatwoot: {e}")

    if resp.status_code >= 400:
        detail = _extract_error_detail(resp)
        frappe.log_error(
            title="frappe_chatwoot: Chatwoot API error",
            message=f"GET {url} -> {resp.status_code}\n{resp.text[:2000]}",
        )
        _write_log(
            request_type="API Error",
            endpoint=path,
            status_code=resp.status_code,
            payload=params,
            error=detail,
        )
        raise ChatwootAPIError(f"Chatwoot: {detail}" if detail else f"Chatwoot API returned {resp.status_code} for {path}")

    data = resp.json()
    if ttl > 0:
        frappe.cache().set_value(cache_key, json.dumps(data), expires_in_sec=ttl)
        import os as _os
        if _os.environ.get("CHATWOOT_CACHE_PROBE"):
            _c = frappe.cache()
            _mk = _c.make_key(cache_key)
            print("PROBE mk=%r in_local=%s local_type=%s redis_direct=%r readback=%r" % (
                _mk, _mk in frappe.local.cache, type(frappe.local.cache).__name__,
                _c.get(_mk), _c.get_value(cache_key)))
    return data


def _post(path: str, payload: dict):
    """POST against the Chatwoot account API. Never cached (mutation)."""
    settings = _settings()
    token = _api_token(settings)
    url = f"{settings.base_url}/api/v1/accounts/{settings.account_id}{path}"
    try:
        resp = requests.post(
            url,
            headers={"api_access_token": token},
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        frappe.log_error(title="frappe_chatwoot: upstream request failed", message=str(e))
        _write_log(request_type="Send Message", endpoint=path, payload=payload, error=str(e)[:2000])
        raise ChatwootAPIError(f"Could not reach Chatwoot: {e}")

    if resp.status_code >= 400:
        detail = _extract_error_detail(resp)
        frappe.log_error(
            title="frappe_chatwoot: Chatwoot API error",
            message=f"POST {url} -> {resp.status_code}\n{resp.text[:2000]}",
        )
        _write_log(
            request_type="Send Message",
            endpoint=path,
            status_code=resp.status_code,
            payload=payload,
            error=detail,
        )
        raise ChatwootAPIError(f"Chatwoot: {detail}" if detail else f"Chatwoot API returned {resp.status_code} for {path}")

    # A mutation invalidates any cached reads for the affected conversation —
    # simplest correct approach given the low write volume expected (a human
    # agent typing in Frappe, not a bulk sender) is to blow the whole cache
    # rather than trying to selectively invalidate by conversation id.
    clear_cache()
    return resp.json()


# ---------------------------------------------------------------------------
# Public accessors — one function per Chatwoot endpoint shape we consume.
# ---------------------------------------------------------------------------


def list_conversations(*, inbox_id=None, status="all", page=1) -> list[dict]:
    params = {"status": status, "page": page}
    if inbox_id:
        params["inbox_id"] = inbox_id
    data = _get("/conversations", params)
    # Live shape: {"data": {"payload": [...]}} — no top-level "meta" counts
    # despite what Chatwoot's published docs describe.
    return (data.get("data") or {}).get("payload") or []


def get_conversation(conversation_id: int) -> dict:
    # Live shape: bare object, not wrapped in "data".
    return _get(f"/conversations/{conversation_id}")


def get_conversations_for_contact(contact_id: int) -> list[dict]:
    # Live shape: {"payload": [...]} — no "data" wrapper here either
    # (inconsistent with /conversations above; verified live).
    data = _get(f"/contacts/{contact_id}/conversations")
    return data.get("payload") or []


def search_contacts(query: str) -> list[dict]:
    data = _get("/contacts/search", {"q": query})
    return data.get("payload") or []


def list_canned_responses() -> list[dict]:
    """Chatwoot's native quick-reply feature — the equivalent of WhatsApp's
    Meta-approved template system, but scoped to Chatwoot itself (no
    approval workflow, no Meta round-trip; these are account-level saved
    replies an agent picks from a `short_code` list). Live shape confirmed
    (2026-07-29) as a bare array, matching Chatwoot's published docs exactly
    — one of the few endpoints in this client that isn't wrapped
    differently than documented."""
    data = _get("/canned_responses")
    return data if isinstance(data, list) else []


def _drop_private_messages(messages: list[dict]) -> list[dict]:
    """Confidentiality invariant, not a preference: Chatwoot returns
    private/internal agent notes by default on the messages endpoint, and
    the `filter_internal_messages=true` query param is known to be silently
    ignored on some self-hosted Chatwoot builds. Always independently drop
    any message where `private` is true, regardless of what the query param
    did upstream."""
    return [m for m in messages if not m.get("private")]


def list_messages(conversation_id: int, before: int | None = None, after: int | None = None) -> dict:
    """Returns the raw {"meta": {...}, "payload": [...]} shape — callers get
    both the contact/label meta and the message list in one call.

    `before` walks backward (older history / scroll-up UI, immutable pages
    so safe to cache longer). `after` walks forward/ascending (incremental
    polling) — see module docstring for the pagination/drain caveats; this
    single-page function returns exactly what Chatwoot gives you for one
    page. Callers needing a full incremental drain should use
    `list_messages_incremental()` instead of calling this directly with
    `after=`.

    Both directions apply the private-message double-filter (see module
    docstring / `_drop_private_messages`) — the `filter_internal_messages`
    query param plus an independent client-side drop.
    """
    if before and after:
        frappe.throw("list_messages: pass only one of before/after, not both")

    params = {"filter_internal_messages": "true"}
    if before:
        params["before"] = before
    if after:
        params["after"] = after

    # Message lists change frequently (that's the whole point of a live
    # chat panel) — use a much shorter TTL than the default for this one
    # call, rather than the full Settings.cache_ttl_seconds, UNLESS the
    # caller is paging backward through history (before= set), where a
    # longer cache is safe since older pages are immutable. Forward/`after=`
    # polling pages must never be cached — the whole point is to see the
    # newest state on every poll.
    if before:
        ttl = min(_cache_ttl(), 8)
    else:
        ttl = 0

    raw = _get(f"/conversations/{conversation_id}/messages", params, cache_seconds=ttl)
    raw["payload"] = _drop_private_messages(raw.get("payload") or [])
    return raw


def list_messages_incremental(conversation_id: int, since_id: int | None = None) -> dict:
    """Drain-loop incremental fetch: return every message newer than
    `since_id` (exclusive), using the `after=` ascending cursor, bounded by
    MAX_DRAIN_PAGES pages and DRAIN_WALL_CLOCK_SECONDS wall-clock so a
    pathological backlog (bulk import, reconnect after downtime, a rapid
    automation posting >100 messages) can never silently lose messages past
    Chatwoot's 100-row-per-page cap, and can never run away indefinitely
    either.

    Returns:
        {
            "messages": [...],       # de-duplicated, in ascending id order
            "meta": {...},           # meta from the LAST page fetched
            "max_id_seen": int|None, # advance the caller's cursor to this
            "truncated": bool,       # True if the drain hit a bound while
                                      # a page was still full (more data may
                                      # remain for the NEXT poll to pick up)
            "pages_fetched": int,
        }

    An unfinished/truncated drain is always safe to resume on the next poll
    cycle: the cursor (`max_id_seen`) only ever advances over message ids
    we've actually confirmed we received, so no message can be skipped —
    worst case is a bounded delay in surfacing a very large backlog, never
    silent loss.
    """
    if since_id is None:
        # No prior cursor — caller should seed one from a normal (non-
        # incremental) load; without a cursor there is nothing to drain
        # against, since `after=` requires a message id to anchor on.
        raw = list_messages(conversation_id)
        messages = raw.get("payload") or []
        max_id = max((m.get("id") for m in messages if m.get("id") is not None), default=None)
        return {
            "messages": messages,
            "meta": raw.get("meta") or {},
            "max_id_seen": max_id,
            "truncated": False,
            "pages_fetched": 1,
        }

    all_messages: list[dict] = []
    seen_ids: set = set()
    cursor = since_id
    last_meta: dict = {}
    truncated = False
    pages_fetched = 0
    start_time = time.monotonic()

    while pages_fetched < MAX_DRAIN_PAGES:
        if time.monotonic() - start_time > DRAIN_WALL_CLOCK_SECONDS:
            truncated = True
            break

        page = list_messages(conversation_id, after=cursor)
        pages_fetched += 1
        page_messages = page.get("payload") or []
        last_meta = page.get("meta") or {}

        for m in page_messages:
            mid = m.get("id")
            if mid is not None and mid in seen_ids:
                continue
            if mid is not None:
                seen_ids.add(mid)
            all_messages.append(m)

        page_ids = [m.get("id") for m in page_messages if m.get("id") is not None]
        if page_ids:
            cursor = max(cursor, max(page_ids))

        if len(page_messages) < CHATWOOT_PAGE_SIZE:
            # Short page: this is the end of the currently-available delta.
            break

        # Page came back full (== CHATWOOT_PAGE_SIZE) — ambiguous, could be
        # more. Keep draining unless we've hit a bound, in which case flag
        # truncated so the caller/next poll knows there may be more still
        # waiting and should NOT treat this as "caught up".
        if pages_fetched >= MAX_DRAIN_PAGES:
            truncated = True

    all_messages.sort(key=lambda m: m.get("id") or 0)

    if truncated:
        _write_log(
            request_type="Webhook Poll",
            endpoint=f"/conversations/{conversation_id}/messages",
            conversation_id=conversation_id,
            payload={"since_id": since_id, "pages_fetched": pages_fetched, "cursor": cursor},
            error="Message drain hit MAX_DRAIN_PAGES/wall-clock bound; resuming next poll.",
        )

    return {
        "messages": all_messages,
        "meta": last_meta,
        "max_id_seen": cursor if (all_messages or cursor != since_id) else since_id,
        "truncated": truncated,
        "pages_fetched": pages_fetched,
    }


def create_message(conversation_id: int, content: str, private: bool = False) -> dict:
    return _post(
        f"/conversations/{conversation_id}/messages",
        {"content": content, "message_type": "outgoing", "private": private},
    )


def get_profile() -> dict:
    """GET /api/v1/profile — not account-scoped. Used to fetch/refresh the
    service agent's pubsub_token for the realtime bridge, and as the
    connectivity check behind Chatwoot Settings' "Test Connection" button."""
    settings = _settings()
    token = _api_token(settings)
    url = f"{settings.base_url}/api/v1/profile"
    try:
        resp = requests.get(url, headers={"api_access_token": token}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        _write_log(request_type="API Error", endpoint="/profile", error=str(e)[:2000])
        raise ChatwootAPIError(f"Could not reach Chatwoot: {e}")

    if resp.status_code >= 400:
        detail = _extract_error_detail(resp)
        _write_log(
            request_type="API Error",
            endpoint="/profile",
            status_code=resp.status_code,
            error=detail,
        )
        raise ChatwootAPIError(f"Chatwoot: {detail}" if detail else f"Chatwoot /profile returned {resp.status_code}")
    return resp.json()
