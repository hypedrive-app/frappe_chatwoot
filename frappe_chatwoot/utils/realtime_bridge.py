# Copyright (c) 2026, Hypedrive
# License: MIT
"""
Realtime bridge: Chatwoot conversation activity -> frappe.publish_realtime.

## Why polling, not a persistent ActionCable connection

The research doc (chatwoot_api_reference.md, section 3) confirms Chatwoot
exposes an ActionCable WebSocket (`wss://.../cable`) that an external client
can subscribe to with the service agent's `pubsub_token`, receiving
`message.created` / `conversation.status_changed` events account-wide. That
is the architecturally "best" way to get near-real-time updates without
polling REST.

However, that requires a LONG-LIVED process holding one open WebSocket
connection for the app's entire lifetime — distinct from anything Frappe's
own process model runs today:
  - `bench worker` processes (RQ workers) are short-task consumers, not
    designed to hold a single persistent external connection for their
    entire lifetime; doing so would tie up a whole worker slot indefinitely
    and doesn't survive a `bench restart`/deploy cycle without explicit
    reconnect-supervision logic.
  - This deployment is Docker/Dokploy-managed (see ampere-server-inventory /
    dokploy-monorepo-webhook-drops-apps in memory) with a fixed docker-compose
    service topology (web/socketio/worker/scheduler/db/redis containers).
    Adding a NEW standalone service (a small always-on Python process just to
    hold the ActionCable connection) is possible but is a docker-compose /
    apps.json-adjacent infrastructure change outside what a Frappe app's own
    install can provision — it would need a matching change to the CRM
    compose's docker-compose.yml, reviewed and deployed independently of this
    app's code.
  - Frappe's own scheduler (`bench schedule`) already runs a supervised,
    auto-restarting periodic-job loop with no additional infra — cron-style
    entries in hooks.py are picked up automatically once this app is
    installed, no compose changes needed.

Given the user's explicit prior guidance (`infra-cold-start-race-class`,
`wallet-service-audit-ctx-cancel` in memory) to be wary of long-lived
goroutines/processes capturing stale context and needing careful
supervision, and given Chatwoot's own confirmed absence of rate-limit
headers (so a cheap, low-frequency poll costs it nothing), the pragmatic
choice for a first, deployable-today version is:

    A once-a-minute scheduler_event poll of the conversations list,
    diffed against the last-seen (id -> updated_at) snapshot in
    frappe.cache(), firing frappe.publish_realtime("chatwoot_message", ...)
    for every conversation whose updated_at advanced.

This is explicitly a fallback, not the final word: `poll_and_broadcast`
below is written so that swapping in a real persistent ActionCable listener
later (as a proper standalone worker process, once a compose-level change is
approved) only requires replacing what CALLS `_broadcast_conversation_update`
— the broadcast contract and payload shape stay identical, so no CRM/FE
consumer code would need to change on that upgrade.

## Broadcast contract

Event name: "chatwoot_message"
Payload: {"conversation_id": int, "inbox_id": int, "updated_at": int}

Deliberately minimal (IDs + timestamp only, no message body) — mirrors
frappe_whatsapp/CRM's own pattern (see frappe_whatsapp_pattern.md section 3):
the realtime event is a "go refetch" signal, not the data itself. The
frontend re-queries `frappe_chatwoot.api.chatwoot.get_messages` (the live,
authoritative source) rather than trusting anything carried on the socket.
Site-wide fan-out (no user/room scoping), same documented-intentional
pattern as frappe_whatsapp's own webhook.py and CRM's WhatsApp on_update
hook — clients filter client-side by conversation/reference match.
"""

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw

_SNAPSHOT_CACHE_KEY = "frappe_chatwoot:v1:realtime:last_seen"


def poll_and_broadcast():
    """Scheduler entrypoint (see hooks.py `scheduler_events["cron"]`)."""
    if not frappe.db.exists("DocType", "Chatwoot Settings"):
        return
    settings = frappe.get_single("Chatwoot Settings")
    if not settings.enabled:
        return

    try:
        conversations = cw.list_conversations(inbox_id=settings.default_inbox_id or None)
    except cw.ChatwootAPIError:
        # Transient upstream failure — log already happened inside the
        # client; don't let a scheduler job raise (would show as a failed
        # scheduled job every minute and spam error logs for a blip).
        return

    last_seen = frappe.cache().get_value(_SNAPSHOT_CACHE_KEY) or {}
    if not isinstance(last_seen, dict):
        last_seen = {}

    changed = {}
    for conv in conversations:
        conv_id = str(conv["id"])
        updated_at = conv.get("updated_at") or conv.get("last_activity_at") or 0
        if last_seen.get(conv_id) != updated_at:
            changed[conv_id] = updated_at
            _broadcast_conversation_update(conv)

    if changed:
        last_seen.update(changed)
        frappe.cache().set_value(_SNAPSHOT_CACHE_KEY, last_seen, expires_in_sec=86400)


def _broadcast_conversation_update(conversation: dict):
    frappe.publish_realtime(
        "chatwoot_message",
        {
            "conversation_id": conversation["id"],
            "inbox_id": conversation.get("inbox_id"),
            "updated_at": conversation.get("updated_at") or conversation.get("last_activity_at"),
        },
    )
