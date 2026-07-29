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
