from . import __version__ as app_version

app_name = "frappe_chatwoot"
app_title = "Frappe Chatwoot"
app_publisher = "Hypedrive"
app_description = "Live-read Chatwoot conversation integration for Frappe. Chatwoot stays the system of record — no message duplication."
app_email = "9.shivamgupta.6@gmail.com"
app_license = "MIT"

# No custom Desk-wide JS needed today — the CRM-side integration (if/when the
# tab-embedded contract lands) lives inside the CRM frontend bundle itself,
# exactly like frappe_whatsapp's own app_include_js is unrelated to CRM's
# WhatsApp tab (that tab is compiled into CRM's own frontend, not loaded via
# this hook). Left here, commented, as the documented extension point:
# app_include_js = "/assets/frappe_chatwoot/js/frappe_chatwoot.js"

# ---------------------------------------------------------------------------
# Scheduler events
# ---------------------------------------------------------------------------
# Chatwoot's ActionCable feed is the "live" signal (see utils/realtime_bridge.py
# for why a persistent WS connection is NOT run as a bench worker in this
# deployment). Short-interval cron poll is the fallback that keeps
# publish_realtime signals flowing without a long-lived process. See the
# module docstring in realtime_bridge.py for the full tradeoff writeup.
scheduler_events = {
    "cron": {
        # every minute — cheap: HEAD-weight conversations list call, cached
        # response compared by (conversation_id, updated_at) to detect deltas.
        "* * * * *": [
            "frappe_chatwoot.utils.realtime_bridge.poll_and_broadcast",
        ],
    },
}

# ---------------------------------------------------------------------------
# doc_events
# ---------------------------------------------------------------------------
# Intentionally empty here. Unlike frappe_whatsapp (which owns a Message
# doctype other apps hook into), frappe_chatwoot owns no conversation/message
# doctype at all — there is nothing of ours for another app's doc_events to
# subscribe to, and we have no local doctype whose writes need reacting to.
# Any CRM-side reaction (e.g. notifying an assigned user on a new inbound
# Chatwoot message) is driven off the realtime broadcast this app emits
# (event name: "chatwoot_message"), not off a doc_events hook.
doc_events = {}

# ---------------------------------------------------------------------------
# Fixtures / boilerplate hook surface — none needed today.
# ---------------------------------------------------------------------------
