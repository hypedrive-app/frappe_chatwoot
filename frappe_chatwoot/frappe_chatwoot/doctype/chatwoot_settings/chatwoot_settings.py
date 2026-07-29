# Copyright (c) 2026, Hypedrive
# License: MIT

import frappe
from frappe.model.document import Document


class ChatwootSettings(Document):
    def validate(self):
        self.base_url = (self.base_url or "").rstrip("/")

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
