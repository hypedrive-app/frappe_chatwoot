# Copyright (c) 2026, Hypedrive
# License: MIT

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import chatwoot_client as cw


def _configure_settings(**overrides):
    settings = frappe.get_single("Chatwoot Settings")
    settings.enabled = 1
    settings.base_url = overrides.get("base_url", "https://support.hypedrive.app")
    settings.account_id = overrides.get("account_id", 1)
    settings.api_token = overrides.get("api_token", "dummy-token-for-test")
    settings.cache_ttl_seconds = overrides.get("cache_ttl_seconds", 20)
    settings.save()
    return settings


def _mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(json_body) if json_body is not None else "")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no JSON body")
    return resp


class TestChatwootClientSettings(FrappeTestCase):
    """Settings-gating behavior (_settings/_api_token) — not HTTP-mocked."""

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    def test_disabled_raises_not_configured(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        with self.assertRaises(cw.ChatwootNotConfigured):
            cw._settings()

    def test_missing_token_raises_not_configured(self):
        from frappe.utils.password import remove_encrypted_password

        _configure_settings()
        # Password fields live in __Auth, not the doctype's own table, so
        # db.set_value on api_token does not clear what get_password() reads.
        remove_encrypted_password("Chatwoot Settings", "Chatwoot Settings", "api_token")
        frappe.db.set_value("Chatwoot Settings", "Chatwoot Settings", "api_token", "")
        # Re-fetch after both writes: get_password() returns the in-memory field
        # when it is set (base_document.get_password), so a doc loaded earlier
        # still carries the old token and the guard never fires.
        settings = frappe.get_single("Chatwoot Settings")
        with self.assertRaises(cw.ChatwootNotConfigured):
            cw._api_token(settings)


class TestChatwootClientCaching(FrappeTestCase):
    def setUp(self):
        _configure_settings(cache_ttl_seconds=20)
        cw.clear_cache()

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_cache_hit_avoids_second_http_call(self, mock_get):
        import json as _json
        mock_get.return_value = _mock_response(200, {"data": {"payload": []}})

        cw.list_conversations()
        _k = cw._cache_key("get", "/conversations", _json.dumps({"page": 1, "status": "all"}, sort_keys=True))
        print("PROBE key:", _k)
        print("PROBE after first call, get_value ->", repr(frappe.cache().get_value(_k)))
        print("PROBE ttl:", cw._cache_ttl())
        cw.list_conversations()

        self.assertEqual(mock_get.call_count, 1)

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_cache_ttl_zero_disables_caching(self, mock_get):
        mock_get.return_value = _mock_response(200, {"data": {"payload": []}})

        cw._get("/conversations", {"status": "all"}, cache_seconds=0)
        cw._get("/conversations", {"status": "all"}, cache_seconds=0)

        self.assertEqual(mock_get.call_count, 2)

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_after_param_is_never_cached(self, mock_get):
        """Forward/after= polling pages must always hit upstream fresh —
        caching them would mean a poll never sees new messages."""
        mock_get.return_value = _mock_response(200, {"meta": {}, "payload": []})

        cw.list_messages(1, after=5)
        cw.list_messages(1, after=5)

        self.assertEqual(mock_get.call_count, 2)

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_before_param_uses_longer_cache(self, mock_get):
        """Backward-paging (before=) pages are immutable history and safe
        to cache — repeated calls with the same before= should hit cache."""
        mock_get.return_value = _mock_response(200, {"meta": {}, "payload": []})

        cw.list_messages(1, before=100)
        cw.list_messages(1, before=100)

        self.assertEqual(mock_get.call_count, 1)


class TestChatwootClientEnvelopeUnwrapping(FrappeTestCase):
    def setUp(self):
        _configure_settings()
        cw.clear_cache()

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_list_conversations_unwraps_data_payload(self, mock_get):
        mock_get.return_value = _mock_response(
            200, {"data": {"payload": [{"id": 1}, {"id": 2}]}}
        )
        result = cw.list_conversations()
        self.assertEqual([c["id"] for c in result], [1, 2])

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_get_conversations_for_contact_unwraps_bare_payload(self, mock_get):
        """Different envelope shape than list_conversations — no "data" key
        at all, just {"payload": [...]}. Must not assume one convention."""
        mock_get.return_value = _mock_response(200, {"payload": [{"id": 7}]})
        result = cw.get_conversations_for_contact(42)
        self.assertEqual([c["id"] for c in result], [7])

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_list_conversations_missing_data_key_returns_empty(self, mock_get):
        mock_get.return_value = _mock_response(200, {})
        result = cw.list_conversations()
        self.assertEqual(result, [])


class TestChatwootClientErrorHandling(FrappeTestCase):
    def setUp(self):
        _configure_settings()
        cw.clear_cache()

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_4xx_raises_with_message_detail(self, mock_get):
        mock_get.return_value = _mock_response(422, {"message": "Content can't be blank"})
        with self.assertRaises(cw.ChatwootAPIError) as ctx:
            cw._get("/conversations/5/messages", cache_seconds=0)
        self.assertIn("Content can't be blank", str(ctx.exception))

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_4xx_raises_with_error_key_detail(self, mock_get):
        mock_get.return_value = _mock_response(401, {"error": "Invalid access token"})
        with self.assertRaises(cw.ChatwootAPIError) as ctx:
            cw._get("/conversations", cache_seconds=0)
        self.assertIn("Invalid access token", str(ctx.exception))

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_4xx_raises_with_errors_list_detail(self, mock_get):
        mock_get.return_value = _mock_response(422, {"errors": ["Phone number invalid"]})
        with self.assertRaises(cw.ChatwootAPIError) as ctx:
            cw._get("/contacts/search", cache_seconds=0)
        self.assertIn("Phone number invalid", str(ctx.exception))

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_non_json_error_body_falls_back_to_clipped_text(self, mock_get):
        mock_get.return_value = _mock_response(
            502, text="<html><body>Bad Gateway</body></html>"
        )
        with self.assertRaises(cw.ChatwootAPIError) as ctx:
            cw._get("/conversations", cache_seconds=0)
        self.assertIn("Bad Gateway", str(ctx.exception))

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_error_writes_chatwoot_log_when_doctype_exists(self, mock_get):
        if not frappe.db.exists("DocType", "Chatwoot Log"):
            self.skipTest("Chatwoot Log doctype not installed in this test env")
        mock_get.return_value = _mock_response(422, {"message": "boom"})
        before_count = frappe.db.count("Chatwoot Log")
        with self.assertRaises(cw.ChatwootAPIError):
            cw._get("/conversations", cache_seconds=0)
        after_count = frappe.db.count("Chatwoot Log")
        self.assertGreater(after_count, before_count)

        latest = frappe.get_last_doc("Chatwoot Log", filters={"request_type": "API Error"})
        self.assertEqual(latest.status_code, 422)
        self.assertIn("boom", latest.error)


class TestChatwootClientPrivateMessageFilter(FrappeTestCase):
    def setUp(self):
        _configure_settings()
        cw.clear_cache()

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    def test_drop_private_messages_pure_function(self):
        messages = [
            {"id": 1, "private": False, "content": "hi"},
            {"id": 2, "private": True, "content": "internal note"},
            {"id": 3, "content": "no private key at all"},
        ]
        result = cw._drop_private_messages(messages)
        self.assertEqual([m["id"] for m in result], [1, 3])

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_list_messages_drops_private_even_if_query_param_ignored(self, mock_get):
        """Simulates a self-hosted Chatwoot build that silently ignores
        filter_internal_messages=true and returns private notes anyway —
        the client-side drop must still remove them."""
        mock_get.return_value = _mock_response(
            200,
            {
                "meta": {},
                "payload": [
                    {"id": 1, "private": False, "content": "visible"},
                    {"id": 2, "private": True, "content": "should never leak"},
                ],
            },
        )
        result = cw.list_messages(1, after=0)
        contents = [m["content"] for m in result["payload"]]
        self.assertEqual(contents, ["visible"])
        self.assertNotIn("should never leak", contents)

    @patch("frappe_chatwoot.utils.chatwoot_client.requests.get")
    def test_list_messages_sends_filter_internal_messages_param(self, mock_get):
        mock_get.return_value = _mock_response(200, {"meta": {}, "payload": []})
        cw.list_messages(1, after=0)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"].get("filter_internal_messages"), "true")


class TestChatwootClientDrainLoop(FrappeTestCase):
    """Coverage for list_messages_incremental — the after=-cursor drain loop
    that fixes the >100-message silent-loss bug (see module docstring)."""

    def setUp(self):
        _configure_settings()
        cw.clear_cache()

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()
        cw.clear_cache()

    def _page(self, ids):
        return {
            "meta": {},
            "payload": [{"id": i, "private": False, "content": f"msg-{i}"} for i in ids],
        }

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_single_short_page_no_drain_needed(self, mock_list_messages):
        mock_list_messages.return_value = self._page([101, 102, 103])

        result = cw.list_messages_incremental(1, since_id=100)

        self.assertEqual(mock_list_messages.call_count, 1)
        self.assertEqual([m["id"] for m in result["messages"]], [101, 102, 103])
        self.assertEqual(result["max_id_seen"], 103)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["pages_fetched"], 1)

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_full_page_then_short_page_drains_both(self, mock_list_messages):
        """A page returning exactly CHATWOOT_PAGE_SIZE rows is ambiguous —
        must re-fetch with the cursor advanced until a short page appears."""
        full_page_ids = list(range(1, cw.CHATWOOT_PAGE_SIZE + 1))
        second_page_ids = [cw.CHATWOOT_PAGE_SIZE + 1, cw.CHATWOOT_PAGE_SIZE + 2]

        mock_list_messages.side_effect = [
            self._page(full_page_ids),
            self._page(second_page_ids),
        ]

        result = cw.list_messages_incremental(1, since_id=0)

        self.assertEqual(mock_list_messages.call_count, 2)
        self.assertEqual(len(result["messages"]), len(full_page_ids) + len(second_page_ids))
        self.assertEqual(result["max_id_seen"], cw.CHATWOOT_PAGE_SIZE + 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["pages_fetched"], 2)

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_drain_bounded_by_max_pages_marks_truncated(self, mock_list_messages):
        """A backlog bigger than MAX_DRAIN_PAGES * 100 rows must stop
        draining (never hang the caller) and flag truncated=True so the next
        poll knows to keep going rather than assuming it's caught up."""
        full_page = self._page(list(range(1, cw.CHATWOOT_PAGE_SIZE + 1)))
        # Every page comes back full — simulates an arbitrarily large backlog.
        mock_list_messages.side_effect = [full_page] * (cw.MAX_DRAIN_PAGES + 2)

        result = cw.list_messages_incremental(1, since_id=0)

        self.assertEqual(mock_list_messages.call_count, cw.MAX_DRAIN_PAGES)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["pages_fetched"], cw.MAX_DRAIN_PAGES)
        # Cursor still advanced correctly over every confirmed id, so a
        # resumed drain next poll cannot re-see or skip any message.
        self.assertEqual(result["max_id_seen"], cw.CHATWOOT_PAGE_SIZE)

    @patch("frappe_chatwoot.utils.chatwoot_client.time.monotonic")
    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_drain_bounded_by_wall_clock_marks_truncated(self, mock_list_messages, mock_monotonic):
        """Even if pages keep coming back full, a slow upstream must not be
        allowed to run past the wall-clock budget."""
        full_page = self._page(list(range(1, cw.CHATWOOT_PAGE_SIZE + 1)))
        mock_list_messages.return_value = full_page

        # First call establishes start_time; subsequent calls simulate time
        # jumping past the wall-clock budget immediately.
        mock_monotonic.side_effect = [0, 0, cw.DRAIN_WALL_CLOCK_SECONDS + 1]

        result = cw.list_messages_incremental(1, since_id=0)

        self.assertTrue(result["truncated"])
        # Loop should have broken on the wall-clock check before fetching
        # a second page.
        self.assertLessEqual(mock_list_messages.call_count, 1)

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_drain_deduplicates_overlapping_ids(self, mock_list_messages):
        """If the same id shows up in two pages (cursor edge case), the
        result must not contain duplicates."""
        mock_list_messages.side_effect = [
            self._page(list(range(1, cw.CHATWOOT_PAGE_SIZE + 1))),
            self._page([cw.CHATWOOT_PAGE_SIZE, cw.CHATWOOT_PAGE_SIZE + 1]),  # overlaps last id
        ]

        result = cw.list_messages_incremental(1, since_id=0)

        ids = [m["id"] for m in result["messages"]]
        self.assertEqual(len(ids), len(set(ids)))

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_no_since_id_does_a_plain_fetch(self, mock_list_messages):
        """Without a prior cursor there's nothing to drain against — a
        normal (non-incremental) load seeds the first cursor instead."""
        mock_list_messages.return_value = self._page([1, 2, 3])

        result = cw.list_messages_incremental(1, since_id=None)

        mock_list_messages.assert_called_once_with(1)
        self.assertEqual(result["max_id_seen"], 3)
        self.assertEqual(result["pages_fetched"], 1)

    @patch("frappe_chatwoot.utils.chatwoot_client.list_messages")
    def test_no_new_messages_returns_empty_and_keeps_cursor(self, mock_list_messages):
        mock_list_messages.return_value = self._page([])

        result = cw.list_messages_incremental(1, since_id=500)

        self.assertEqual(result["messages"], [])
        self.assertEqual(result["max_id_seen"], 500)
        self.assertFalse(result["truncated"])
