# Copyright (c) 2026, Hypedrive
# License: MIT

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import chatwoot_client as cw
from frappe_chatwoot.utils import realtime_bridge


def _configure_settings(enabled=1):
    settings = frappe.get_single("Chatwoot Settings")
    settings.enabled = enabled
    settings.base_url = "https://support.hypedrive.app"
    settings.account_id = 1
    settings.api_token = "dummy-token-for-test"
    settings.save()
    return settings


class TestPollAndBroadcast(FrappeTestCase):
    def setUp(self):
        frappe.cache().delete_value(realtime_bridge._SNAPSHOT_CACHE_KEY)

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        frappe.cache().delete_value(realtime_bridge._SNAPSHOT_CACHE_KEY)

    def test_noop_when_disabled(self):
        _configure_settings(enabled=0)
        with patch("frappe_chatwoot.utils.chatwoot_client.list_conversations") as mock_list:
            realtime_bridge.poll_and_broadcast()
        mock_list.assert_not_called()

    @patch("frappe_chatwoot.utils.realtime_bridge.frappe.publish_realtime")
    @patch("frappe_chatwoot.utils.chatwoot_client.list_conversations")
    def test_broadcasts_only_changed_conversations(self, mock_list, mock_publish):
        _configure_settings(enabled=1)
        mock_list.return_value = [
            {"id": 1, "inbox_id": 10, "updated_at": 100},
            {"id": 2, "inbox_id": 10, "updated_at": 200},
        ]

        realtime_bridge.poll_and_broadcast()
        self.assertEqual(mock_publish.call_count, 2)

        mock_publish.reset_mock()
        # Second poll: conversation 1 unchanged, conversation 2 advanced.
        mock_list.return_value = [
            {"id": 1, "inbox_id": 10, "updated_at": 100},
            {"id": 2, "inbox_id": 10, "updated_at": 250},
        ]
        realtime_bridge.poll_and_broadcast()

        self.assertEqual(mock_publish.call_count, 1)
        event_name, payload = mock_publish.call_args[0]
        self.assertEqual(event_name, "chatwoot_message")
        self.assertEqual(payload["conversation_id"], 2)
        self.assertEqual(payload["updated_at"], 250)

    @patch("frappe_chatwoot.utils.chatwoot_client.list_conversations")
    def test_swallows_chatwoot_api_error_without_raising(self, mock_list):
        """Scheduler-safety: a transient upstream failure must never
        propagate out of poll_and_broadcast (would show as a failed
        scheduled job every minute)."""
        _configure_settings(enabled=1)
        mock_list.side_effect = cw.ChatwootAPIError("upstream is down")

        try:
            realtime_bridge.poll_and_broadcast()
        except cw.ChatwootAPIError:
            self.fail("poll_and_broadcast must swallow ChatwootAPIError, not raise it")

    @patch("frappe_chatwoot.utils.chatwoot_client.list_conversations")
    def test_failed_poll_writes_chatwoot_log_when_doctype_exists(self, mock_list):
        if not frappe.db.exists("DocType", "Chatwoot Log"):
            self.skipTest("Chatwoot Log doctype not installed in this test env")
        _configure_settings(enabled=1)
        mock_list.side_effect = cw.ChatwootAPIError("upstream is down")

        before_count = frappe.db.count("Chatwoot Log")
        realtime_bridge.poll_and_broadcast()
        after_count = frappe.db.count("Chatwoot Log")

        self.assertGreater(after_count, before_count)
        latest = frappe.get_last_doc("Chatwoot Log", filters={"request_type": "Webhook Poll"})
        self.assertIn("upstream is down", latest.error)

    @patch("frappe_chatwoot.utils.realtime_bridge.frappe.publish_realtime")
    @patch("frappe_chatwoot.utils.chatwoot_client.list_conversations")
    def test_falls_back_to_last_activity_at_when_updated_at_missing(self, mock_list, mock_publish):
        _configure_settings(enabled=1)
        mock_list.return_value = [{"id": 5, "inbox_id": 10, "last_activity_at": 999}]

        realtime_bridge.poll_and_broadcast()

        mock_publish.assert_called_once()
        _, payload = mock_publish.call_args[0]
        self.assertEqual(payload["updated_at"], 999)
