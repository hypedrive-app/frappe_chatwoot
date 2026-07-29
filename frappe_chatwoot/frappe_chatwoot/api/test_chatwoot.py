# Copyright (c) 2026, Hypedrive
# License: MIT

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.frappe_chatwoot.api import chatwoot as api
from frappe_chatwoot.utils import chatwoot_client as cw


def _configure_settings(enabled=1):
    settings = frappe.get_single("Chatwoot Settings")
    settings.enabled = enabled
    settings.base_url = "https://support.hypedrive.app"
    settings.account_id = 1
    settings.api_token = "dummy-token-for-test"
    settings.save()
    return settings


class TestChatwootApiGracefulDegradation(FrappeTestCase):
    """When Chatwoot Settings is missing/disabled, every read-path API
    should degrade softly (return empty/False) rather than raising — this
    is the documented contract shared with crm.api.whatsapp's own
    graceful-degradation behavior."""

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_is_chatwoot_enabled_false_when_disabled(self):
        _configure_settings(enabled=0)
        self.assertFalse(api.is_chatwoot_enabled())

    def test_is_chatwoot_enabled_true_when_configured(self):
        _configure_settings(enabled=1)
        self.assertTrue(api.is_chatwoot_enabled())

    def test_is_chatwoot_installed_true(self):
        # frappe_chatwoot is installed in this test site by definition.
        self.assertTrue(api.is_chatwoot_installed())

    def test_get_conversations_for_contact_degrades_to_empty_list_when_disabled(self):
        _configure_settings(enabled=0)
        result = api.get_conversations_for_contact("CRM Lead", "does-not-matter")
        self.assertEqual(result, [])

    def test_get_messages_throws_when_disabled(self):
        """Unlike the conversation-list lookup (soft-degrade to []),
        get_messages requires an already-resolved conversation_id and
        throws rather than silently returning an empty page — mirrors the
        documented contract in api/chatwoot.py's docstring."""
        _configure_settings(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            api.get_messages(1)

    def test_send_message_throws_when_disabled(self):
        _configure_settings(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "hello")


class TestChatwootApiRoleGate(FrappeTestCase):
    """validate_access enforces ALLOWED_ROLES for non-Administrator users."""

    def setUp(self):
        _configure_settings(enabled=1)
        self.test_user = "test_chatwoot_role_gate@example.com"
        if not frappe.db.exists("User", self.test_user):
            frappe.get_doc({
                "doctype": "User",
                "email": self.test_user,
                "first_name": "Chatwoot",
                "last_name": "RoleGateTest",
                "send_welcome_email": 0,
                "roles": [{"role": "Sales Manager"}],
            }).insert(ignore_permissions=True)

        self.lead = frappe.get_doc({
            "doctype": "CRM Lead" if frappe.db.exists("DocType", "CRM Lead") else "Contact",
        })

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_missing_reference_raises(self):
        with self.assertRaises(frappe.ValidationError):
            api.validate_access("", "")

    def test_role_outside_allowlist_raises_permission_error(self):
        if not frappe.db.exists("DocType", "CRM Lead"):
            self.skipTest("CRM Lead doctype not installed in this test env")

        no_access_user = "test_chatwoot_no_access@example.com"
        if not frappe.db.exists("User", no_access_user):
            frappe.get_doc({
                "doctype": "User",
                "email": no_access_user,
                "first_name": "NoAccess",
                "last_name": "Test",
                "send_welcome_email": 0,
                "roles": [{"role": "Guest"}] if frappe.db.exists("Role", "Guest") else [],
            }).insert(ignore_permissions=True)

        lead = frappe.get_doc({
            "doctype": "CRM Lead",
            "lead_name": "Chatwoot Role Gate Test Lead",
        }).insert(ignore_permissions=True)

        with patch.object(frappe, "session") as mock_session:
            mock_session.user = no_access_user
            with self.assertRaises(frappe.PermissionError):
                api.validate_access("CRM Lead", lead.name)

        frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)

    def test_administrator_bypasses_role_allowlist(self):
        if not frappe.db.exists("DocType", "CRM Lead"):
            self.skipTest("CRM Lead doctype not installed in this test env")

        lead = frappe.get_doc({
            "doctype": "CRM Lead",
            "lead_name": "Chatwoot Admin Bypass Test Lead",
        }).insert(ignore_permissions=True)

        # Administrator is exempted from the ALLOWED_ROLES gate explicitly
        # in validate_access's condition.
        doc = api.validate_access("CRM Lead", lead.name)
        self.assertEqual(doc.name, lead.name)

        frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)


class TestChatwootApiMessageDirectionMapping(FrappeTestCase):
    def test_annotate_direction_maps_all_known_codes(self):
        messages = [
            {"id": 1, "message_type": 0},
            {"id": 2, "message_type": 1},
            {"id": 3, "message_type": 2},
            {"id": 4, "message_type": 3},
            {"id": 5, "message_type": 99},
        ]
        result = api._annotate_direction(messages)
        directions = {m["id"]: m["direction"] for m in result}
        self.assertEqual(directions, {
            1: "incoming",
            2: "outgoing",
            3: "activity",
            4: "template",
            5: "unknown",
        })


class TestChatwootApiSendMessageValidation(FrappeTestCase):
    def setUp(self):
        _configure_settings(enabled=1)

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_empty_content_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "")

    def test_whitespace_only_content_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "   ")

    @patch("frappe_chatwoot.utils.chatwoot_client.create_message")
    def test_content_is_stripped_before_send(self, mock_create):
        mock_create.return_value = {"id": 1}
        api.send_message(1, "  hello world  ")
        mock_create.assert_called_once_with(1, "hello world")


class TestConversationEndpointsRoleGate(FrappeTestCase):
    """The conversation_id-keyed endpoints have no reference-doc context to
    check, but must still enforce the role allowlist so they are not an
    unguarded /api/method/ path around crm.api.chatwoot's ownership check."""

    def setUp(self):
        _configure_settings(enabled=1)

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_get_messages_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.get_messages(1)

    def test_get_new_messages_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.get_new_messages(1)

    def test_send_message_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.send_message(1, "hello")

    @patch("frappe_chatwoot.utils.chatwoot_client.create_message")
    def test_send_message_allowed_for_sales_user(self, mock_create):
        mock_create.return_value = {"id": 1}
        with patch("frappe.get_roles", return_value=["Sales User"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "sales@example.com"
                api.send_message(1, "hello")
        mock_create.assert_called_once_with(1, "hello")


class TestChatwootApiClearCache(FrappeTestCase):
    def test_clear_cache_requires_system_manager(self):
        with patch("frappe.get_roles", return_value=["Sales User"]):
            with self.assertRaises(frappe.PermissionError):
                api.clear_chatwoot_cache()

    @patch("frappe_chatwoot.utils.chatwoot_client.clear_cache")
    def test_clear_cache_succeeds_for_system_manager(self, mock_clear):
        with patch("frappe.get_roles", return_value=["System Manager"]):
            result = api.clear_chatwoot_cache()
        self.assertEqual(result, {"ok": True})
        mock_clear.assert_called_once()
