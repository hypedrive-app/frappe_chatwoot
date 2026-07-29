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
- message pagination is via `before=<message id>`, walking backward; there is
  no forward/after cursor (matches the "load older on scroll-up" UI model).
"""

import json

import frappe
import requests

CACHE_PREFIX = "frappe_chatwoot:v1"
DEFAULT_TTL = 20
HTTP_TIMEOUT = 15


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
            return json.loads(cached)

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
        raise ChatwootAPIError(f"Could not reach Chatwoot: {e}")

    if resp.status_code >= 400:
        frappe.log_error(
            title="frappe_chatwoot: Chatwoot API error",
            message=f"GET {url} -> {resp.status_code}\n{resp.text[:2000]}",
        )
        raise ChatwootAPIError(f"Chatwoot API returned {resp.status_code} for {path}")

    data = resp.json()
    if ttl > 0:
        frappe.cache().set_value(cache_key, json.dumps(data), expires_in_sec=ttl)
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
        raise ChatwootAPIError(f"Could not reach Chatwoot: {e}")

    if resp.status_code >= 400:
        frappe.log_error(
            title="frappe_chatwoot: Chatwoot API error",
            message=f"POST {url} -> {resp.status_code}\n{resp.text[:2000]}",
        )
        raise ChatwootAPIError(f"Chatwoot API returned {resp.status_code} for {path}")

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


def list_messages(conversation_id: int, before: int | None = None) -> dict:
    """Returns the raw {"meta": {...}, "payload": [...]} shape — callers get
    both the contact/label meta and the message list in one call."""
    params = {}
    if before:
        params["before"] = before
    # Message lists change frequently (that's the whole point of a live
    # chat panel) — use a much shorter TTL than the default for this one
    # call, rather than the full Settings.cache_ttl_seconds, UNLESS the
    # caller is paging backward through history (before= set), where a
    # longer cache is safe since older pages are immutable.
    ttl = None if before else min(_cache_ttl(), 8)
    return _get(f"/conversations/{conversation_id}/messages", params, cache_seconds=ttl)


def create_message(conversation_id: int, content: str, private: bool = False) -> dict:
    return _post(
        f"/conversations/{conversation_id}/messages",
        {"content": content, "message_type": "outgoing", "private": private},
    )


def get_profile() -> dict:
    """GET /api/v1/profile — not account-scoped. Used to fetch/refresh the
    service agent's pubsub_token for the realtime bridge."""
    settings = _settings()
    token = _api_token(settings)
    url = f"{settings.base_url}/api/v1/profile"
    resp = requests.get(url, headers={"api_access_token": token}, timeout=HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise ChatwootAPIError(f"Chatwoot /profile returned {resp.status_code}")
    return resp.json()
