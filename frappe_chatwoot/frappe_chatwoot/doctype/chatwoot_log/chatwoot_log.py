# Copyright (c) 2026, Hypedrive
# License: MIT

from frappe.model.document import Document


class ChatwootLog(Document):
	"""Append-only audit log for Chatwoot integration activity.

	Mirrors frappe_whatsapp's `WhatsApp Notification Log` shape (minimal
	fields, System-Manager-read-only permission block, no write/create/delete
	keys — see chatwoot_log.json). Written to from:

	1. `utils/chatwoot_client.py::_get()` / `_post()` on any 4xx/5xx upstream
	   response, or a transport-level failure (request_type="API Error").
	2. `utils/chatwoot_client.py::_post()` failures specifically for message
	   sends (request_type="Send Message").
	3. `utils/realtime_bridge.py::poll_and_broadcast()` on a failed poll
	   cycle, and when the incremental message drain hits its page/wall-clock
	   bound (request_type="Webhook Poll").

	This is deliberately NOT a message/conversation doctype — it stores only
	request metadata (endpoint, status code, error text, and whichever body
	is most useful for debugging), never full conversation content, keeping
	it aligned with the app's live-read-only architecture.
	"""

	pass
