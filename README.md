# frappe_chatwoot

Live-read Chatwoot conversation integration for Frappe / Frappe CRM.

Unlike a typical Frappe channel-integration app (e.g. `frappe_whatsapp`), this
app does **not** copy Chatwoot messages into a local Frappe doctype. Chatwoot
remains the single source of truth for conversation/message state. Frappe
queries Chatwoot's REST API live, at view time, with a short-TTL cache to
avoid hammering the Chatwoot instance, and uses Chatwoot's ActionCable
WebSocket feed purely as a cache-invalidation / "go refetch" signal.

## Why live-read instead of copy-on-webhook?

A prior approach (a Server Script relay) copied inbound/outbound WhatsApp
messages into Frappe's `WhatsApp Message` doctype. This created a second copy
of conversation state that could drift from Chatwoot's real thread (message
edits, deletes, agent reassignment, label changes, etc. never reflected back).
This app is a deliberate architectural correction: no message duplication,
Chatwoot's REST API is queried directly.

## Doctypes

- **Chatwoot Settings** (Single) — base URL, API token, account ID, default
  inbox ID. Mirrors `WhatsApp Settings`: fast lookup + doubles as the
  "is this app installed" signal for consuming apps (e.g. CRM) via
  `frappe.db.exists("DocType", "Chatwoot Settings")`.

No message/conversation doctype exists by design — conversations are resolved
live via the Chatwoot Contact API (`chatwoot_contact_id` join key) or a
phone/email search fallback, and messages are fetched live per-conversation.

## API surface (`frappe_chatwoot.api.chatwoot`)

- `is_chatwoot_installed()` / `is_chatwoot_enabled()`
- `get_conversations_for_contact(reference_doctype, reference_name)`
- `get_messages(conversation_id, before=None)`
- `send_message(conversation_id, content)`

All whitelisted, all proxy live to Chatwoot with a short in-process cache
(see `frappe_chatwoot/utils/chatwoot_client.py`).

## Realtime

A scheduler-driven poll bridges Chatwoot's ActionCable `message.created` /
`conversation.status_changed` events into `frappe.publish_realtime` — see
`frappe_chatwoot/utils/realtime_bridge.py` for the chosen approach and why
(no long-lived worker process is available in this deployment's process
model, see doc comment for detail).
