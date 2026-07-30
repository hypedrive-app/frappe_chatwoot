# Copyright (c) 2026, Hypedrive
# License: MIT

import frappe
from frappe.model.document import Document


# Bounds for the self-imposed read cache TTL (Chatwoot exposes no rate-limit
# headers — see chatwoot_client module docstring). Below MIN we hammer upstream;
# above MAX stale conversation reads linger too long. 0 is NOT allowed here
# because the doctype default/intent is a caching integration; the client's own
# `_cache_ttl` still treats a falsy value as DEFAULT_TTL as a second guard.
MIN_CACHE_TTL_SECONDS = 1
MAX_CACHE_TTL_SECONDS = 300
DEFAULT_CACHE_TTL_SECONDS = 20


class ChatwootSettings(Document):
    def validate(self):
        # Normalise first so the checks below see the cleaned values, and so a
        # trailing slash can never sneak into f"{base_url}/api/v1/..." joins in
        # chatwoot_client (that produced "//api" and 404s upstream).
        self.base_url = (self.base_url or "").strip().rstrip("/")

        # Clamp the TTL into a sane range regardless of the enabled flag — a
        # nonsensical value stored while disabled would otherwise take effect
        # silently the moment the integration is turned on.
        self._normalise_cache_ttl()

        # Everything below is a hard requirement only WHEN the integration is
        # enabled. While disabled the settings may legitimately be blank/partial.
        if not self.enabled:
            return

        if not self.base_url:
            frappe.throw(
                "Base URL is required when Chatwoot is enabled "
                "(e.g. https://support.example.com)."
            )
        if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            # A scheme-less host breaks the requests call and the URL join.
            frappe.throw("Base URL must start with http:// or https://.")

        if not self.account_id or int(self.account_id) <= 0:
            frappe.throw("Account ID is required and must be a positive integer when Chatwoot is enabled.")

        if self.default_inbox_id is not None and int(self.default_inbox_id or 0) < 0:
            frappe.throw("Default Inbox ID must be a positive integer, or blank to search all inboxes.")

        # api_token is a Password field — read the decrypted value. get_password
        # returns the unsaved value during validate(), so this catches a token
        # cleared in the same save. Without a token every conversation-read call
        # 401s, so refuse to enable in that state rather than fail lazily later.
        token = self.get_password("api_token", raise_exception=False)
        if not token:
            frappe.throw("API Token is required when Chatwoot is enabled.")

    def _normalise_cache_ttl(self):
        ttl = int(self.cache_ttl_seconds or DEFAULT_CACHE_TTL_SECONDS)
        if ttl < MIN_CACHE_TTL_SECONDS:
            ttl = MIN_CACHE_TTL_SECONDS
        elif ttl > MAX_CACHE_TTL_SECONDS:
            ttl = MAX_CACHE_TTL_SECONDS
        self.cache_ttl_seconds = ttl

    def on_update(self):
        # Settings changed (token rotated, enabled toggled, TTL changed) —
        # drop the in-process/redis read cache so the next read reflects the
        # new config immediately rather than serving a stale cached response
        # under the old token/base_url for up to cache_ttl_seconds.
        from frappe_chatwoot.utils.chatwoot_client import clear_cache

        clear_cache()

    @frappe.whitelist()
    def test_connection(self):
        """Backs the "Test Connection" button in chatwoot_settings.js.
        Calls GET /api/v1/profile with the currently-SAVED credentials (not
        unsaved form edits — the doc must be saved first) and returns the
        connected service agent's identity on success. Any failure raises
        ChatwootAPIError with Chatwoot's real error detail (see
        chatwoot_client._extract_error_detail), which the FE surfaces
        verbatim via frappe.throw."""
        from frappe_chatwoot.utils.chatwoot_client import get_profile

        profile = get_profile()
        return {
            "name": profile.get("name") or profile.get("display_name"),
            "email": profile.get("email"),
        }
