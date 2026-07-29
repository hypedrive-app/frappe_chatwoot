# Copyright (c) 2026, Hypedrive
# License: MIT

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils.chatwoot_client import ChatwootNotConfigured


class TestChatwootSettings(FrappeTestCase):
    def tearDown(self):
        # Leave the Single doc disabled/unconfigured between tests so other
        # test modules don't accidentally see a "configured" state.
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

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

    def test_on_update_clears_cache(self):
        with patch("frappe_chatwoot.utils.chatwoot_client.clear_cache") as mock_clear:
            settings = frappe.get_single("Chatwoot Settings")
            settings.base_url = "https://support.hypedrive.app"
            settings.account_id = 1
            settings.api_token = "dummy-token-for-test"
            settings.save()
            mock_clear.assert_called()

    def test_password_field_round_trips(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.base_url = "https://support.hypedrive.app"
        settings.account_id = 1
        settings.api_token = "super-secret-token"
        settings.save()

        reloaded = frappe.get_single("Chatwoot Settings")
        self.assertEqual(reloaded.get_password("api_token"), "super-secret-token")

    def test_disabled_settings_raise_not_configured(self):
        from frappe_chatwoot.utils.chatwoot_client import _settings

        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

        with self.assertRaises(ChatwootNotConfigured):
            _settings()

    def test_missing_base_url_raises_not_configured(self):
        from frappe_chatwoot.utils.chatwoot_client import _settings

        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 1
        settings.base_url = ""
        settings.account_id = 1
        settings.api_token = "dummy-token-for-test"
        settings.save()

        with self.assertRaises(ChatwootNotConfigured):
            _settings()
