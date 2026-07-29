# Copyright (c) 2026, Hypedrive
# License: MIT

import frappe
from frappe.tests.utils import FrappeTestCase


class TestChatwootSettings(FrappeTestCase):
    def test_base_url_trailing_slash_stripped(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.base_url = "https://support.hypedrive.app/"
        settings.account_id = 1
        settings.api_token = "dummy-token-for-test"
        settings.save()
        self.assertEqual(
            frappe.get_cached_value("Chatwoot Settings", "Chatwoot Settings", "base_url"),
            "https://support.hypedrive.app",
        )
