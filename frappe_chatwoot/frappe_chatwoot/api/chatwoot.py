# Copyright (c) 2026, Hypedrive
# License: MIT
"""
Whitelisted API surface for frappe_chatwoot.

Mirrors the contract shape of crm/api/whatsapp.py (see frappe_whatsapp_pattern.md
research doc, section 3) so a future CRM-side crm/api/chatwoot.py can proxy
these calls the same way CRM proxies frappe_whatsapp's doctype today — except
here there is no local doctype to query with frappe.get_all; every call is a
live Chatwoot API read/write via utils/chatwoot_client.py.

Contact resolution: this Frappe/CRM install has no pre-existing
`chatwoot_contact_id` field synced onto Lead/Deal/Contact (that field exists
in a DIFFERENT integration, the Twenty CRM <-> Chatwoot sync — not this one).
So contact resolution here is live: look up the reference doc's phone/email,
call Chatwoot's /contacts/search, then /contacts/{id}/conversations. This is
the "zero-storage" option the build explicitly preferred over persisting a
mapping doctype, since Chatwoot's search endpoint is fast enough for
interactive (view-time) use and avoids a second place drift can occur.
"""

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw

ALLOWED_ROLES = ["System Manager", "Sales Manager", "Sales User"]


def validate_role():
    """Role gate only — the part of validate_access that does not need a
    reference doc. Split out so the conversation_id-keyed endpoints
    (get_messages/get_new_messages/send_message), which have no reference-doc
    context to check, still enforce the role allowlist when called directly
    via /api/method/ instead of through crm.api.chatwoot's wrapper."""
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(role in user_roles for role in ALLOWED_ROLES) and frappe.session.user != "Administrator":
        frappe.throw("Not permitted to access Chatwoot conversations", frappe.PermissionError)


def validate_access(reference_doctype: str, reference_name: str, permtype: str = "read"):
    """Same shape as crm.api.whatsapp.validate_access: role gate + document-level
    permission check. Raises frappe.PermissionError on failure."""
    if not reference_doctype or not reference_name:
        frappe.throw("reference_doctype and reference_name are required")

    validate_role()

    reference_doc = frappe.get_doc(reference_doctype, reference_name)
    if not reference_doc.has_permission(permtype):
        frappe.throw("Not permitted to access this record", frappe.PermissionError)
    return reference_doc


@frappe.whitelist()
def is_chatwoot_installed() -> bool:
    return frappe.db.exists("DocType", "Chatwoot Settings") is not None


@frappe.whitelist()
def is_chatwoot_enabled() -> bool:
    if not frappe.db.exists("DocType", "Chatwoot Settings"):
        return False
    enabled = frappe.get_cached_value("Chatwoot Settings", "Chatwoot Settings", "enabled")
    base_url = frappe.get_cached_value("Chatwoot Settings", "Chatwoot Settings", "base_url")
    return bool(enabled) and bool(base_url)


def _extract_contact_query(reference_doc) -> list[str]:
    """Pull phone/email candidates off a reference doc, cheapest-first.
    Handles the field-name variance across CRM Lead / CRM Deal / Contact."""
    candidates = []
    for fieldname in ("mobile_no", "phone", "phone_number", "whatsapp_no"):
        val = reference_doc.get(fieldname)
        if val:
            candidates.append(val)
    for fieldname in ("email_id", "email"):
        val = reference_doc.get(fieldname)
        if val:
            candidates.append(val)
    return candidates


@frappe.whitelist()
def get_conversations_for_contact(reference_doctype: str, reference_name: str) -> list[dict]:
    """Resolve live conversations for a Frappe record by searching Chatwoot's
    contact directory by phone/email, then listing that contact's conversations.
    Returns [] gracefully if not installed/enabled/no match — same degrade-soft
    contract as crm.api.whatsapp.get_whatsapp_messages."""
    if not is_chatwoot_enabled():
        return []

    reference_doc = validate_access(reference_doctype, reference_name)

    # CRM Deal special-case, mirroring frappe_whatsapp's own precedent: a Deal
    # typically starts life as a Lead, so early conversation history may only
    # be resolvable via the Deal's linked Lead's contact details.
    query_docs = [reference_doc]
    if reference_doctype == "CRM Deal":
        lead = reference_doc.get("lead")
        if lead and frappe.db.exists("CRM Lead", lead):
            validate_access("CRM Lead", lead)
            query_docs.append(frappe.get_doc("CRM Lead", lead))

    seen_conversation_ids = set()
    results = []
    for doc in query_docs:
        for query in _extract_contact_query(doc):
            try:
                contacts = cw.search_contacts(query)
            except cw.ChatwootAPIError:
                continue
            for contact in contacts:
                try:
                    conversations = cw.get_conversations_for_contact(contact["id"])
                except cw.ChatwootAPIError:
                    continue
                for conv in conversations:
                    if conv["id"] in seen_conversation_ids:
                        continue
                    seen_conversation_ids.add(conv["id"])
                    results.append(conv)

    results.sort(key=lambda c: c.get("last_activity_at") or c.get("timestamp") or 0, reverse=True)
    return results


def _annotate_direction(messages: list[dict]) -> list[dict]:
    # Normalize the numeric message_type into a readable direction, and drop
    # the private/internal-note distinction cleanly for FE consumption.
    # Note: private messages are already stripped by
    # chatwoot_client.list_messages/_drop_private_messages before this point
    # — this is purely a display-shape normalization, not a filtering step.
    type_map = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}
    for m in messages:
        m["direction"] = type_map.get(m.get("message_type"), "unknown")
    return messages


def _extract_assignee(meta: dict) -> dict | None:
    """Flatten Chatwoot's `meta.assignee` (User#push_event_data: id, name,
    available_name, avatar_url, type, availability_status, thumbnail) down to
    the {name, avatar} shape CRM's UI actually needs. Only sourced from the
    messages-index `meta` — the conversations-list endpoint renders its
    assignee through a different jbuilder partial with no avatar_url key at
    all, so reading it from there would silently yield no avatar (verified
    against Chatwoot source, see wa-relay's thread.ts handler for the same
    finding). Returns None (not a dict) when unassigned, so callers can rely
    on truthiness rather than checking for an empty name."""
    assignee = (meta or {}).get("assignee") or {}
    name = (assignee.get("name") or "").strip()
    if not name:
        return None
    avatar = (assignee.get("thumbnail") or assignee.get("avatar_url") or "").strip()
    return {"name": name, "avatar": avatar or None}


def _chatwoot_url(conversation_id: int) -> str | None:
    """Deep link to this conversation in the agent's Chatwoot dashboard:
    {base_url}/app/accounts/{account_id}/conversations/{conversation_id}.
    Composed server-side since both halves are server-only Settings values;
    a caller with only a conversation_id could never assemble this
    correctly. None if Chatwoot isn't configured (defensive; callers already
    gate on is_chatwoot_enabled before reaching here in practice)."""
    try:
        settings = frappe.get_single("Chatwoot Settings")
    except Exception:
        return None
    if not settings.base_url or not settings.account_id:
        return None
    return f"{settings.base_url}/app/accounts/{settings.account_id}/conversations/{conversation_id}"


@frappe.whitelist()
def get_messages(conversation_id: int, before: int = None) -> dict:
    """Live message page for one conversation. Enforces the role allowlist, but
    cannot check reference-doc access on its own — a conversation_id alone
    carries no reference-doc context. Callers that HAVE that context must bind
    the conversation to it before calling: crm.api.chatwoot does this in
    _validate_conversation_ownership, which confirms the id belongs to the
    reference doc's own conversations. Use that wrapper from CRM rather than
    calling this directly."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    conversation_id = frappe.utils.cint(conversation_id)
    raw = cw.list_messages(conversation_id, before=frappe.utils.cint(before) if before else None)
    messages = _annotate_direction(raw.get("payload") or [])
    meta = raw.get("meta") or {}

    result = {
        "meta": meta,
        "messages": messages,
        "chatwoot_url": _chatwoot_url(conversation_id),
    }
    assignee = _extract_assignee(meta)
    if assignee:
        result["assignee"] = assignee
    return result


@frappe.whitelist()
def get_new_messages(conversation_id: int, since_id: int = None) -> dict:
    """Incremental poll: every message newer than `since_id`, using the
    bounded after=-cursor drain loop in chatwoot_client.list_messages_incremental
    (see that function's docstring for the pagination-loss fix this closes).
    Callers should persist `max_id_seen` and pass it back as `since_id` on
    the next poll. `truncated=True` means the drain hit its page/time bound
    with more data potentially still pending — call again immediately (or on
    the very next poll tick) rather than treating this as caught-up.

    Role-gated only — see get_messages' docstring on why reference-doc access
    must be bound by the caller (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    conversation_id = frappe.utils.cint(conversation_id)
    since_id = frappe.utils.cint(since_id) if since_id else None
    result = cw.list_messages_incremental(conversation_id, since_id=since_id)
    result["messages"] = _annotate_direction(result.get("messages") or [])
    return result


@frappe.whitelist()
def send_message(conversation_id: int, content: str) -> dict:
    """Role-gated only — see get_messages' docstring on why reference-doc access
    must be bound by the caller (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    if not content or not content.strip():
        frappe.throw("Message content cannot be empty")
    conversation_id = frappe.utils.cint(conversation_id)
    result = cw.create_message(conversation_id, content.strip())
    return result


@frappe.whitelist()
def toggle_status(conversation_id: int, status: str) -> dict:
    """Resolve/reopen a conversation via Chatwoot's native toggle_status
    endpoint. Role-gated only — see get_messages' docstring on why
    reference-doc access must be bound by the caller
    (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    if status not in ("open", "resolved"):
        frappe.throw("status must be 'open' or 'resolved'")
    conversation_id = frappe.utils.cint(conversation_id)
    cw.toggle_conversation_status(conversation_id, status)
    # Chatwoot's toggle_status response shape isn't guaranteed stable enough
    # to trust for the new status — read the conversation back so the caller
    # gets the authoritative post-toggle state rather than an echo of what it
    # asked for (which could be wrong if the toggle silently no-oped, e.g.
    # already-resolved).
    conversation = cw.get_conversation(conversation_id)
    return {"status": conversation.get("status") or status}


@frappe.whitelist()
def get_canned_responses() -> list[dict]:
    """Role-gated only, same as send_message — canned responses are an
    account-level list, not bound to any specific reference doc."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    return cw.list_canned_responses()


def _default_inbox_id() -> int:
    settings = frappe.get_single("Chatwoot Settings")
    inbox_id = settings.default_inbox_id
    if not inbox_id:
        frappe.throw(
            "Chatwoot Settings has no Default Inbox ID configured — required to look up "
            "WhatsApp message templates."
        )
    return frappe.utils.cint(inbox_id)


@frappe.whitelist()
def get_templates(inbox_id: int = None) -> list[dict]:
    """Chatwoot-native WhatsApp template list, sourced straight from the
    inbox's own `message_templates` array (already synced from Meta by
    Chatwoot — see chatwoot_client.get_inbox docstring). Role-gated only,
    same posture as get_canned_responses: templates are account/inbox-level,
    not bound to any specific reference doc.

    `inbox_id` defaults to Chatwoot Settings.default_inbox_id when omitted."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    resolved_inbox_id = frappe.utils.cint(inbox_id) if inbox_id else _default_inbox_id()
    return cw.list_message_templates(resolved_inbox_id)


@frappe.whitelist()
def send_template(
    conversation_id: int,
    template_name: str,
    category: str,
    language: str,
    processed_params: dict | str | None = None,
) -> dict:
    """Send a WhatsApp template message through Chatwoot's own native
    /messages endpoint with a `template_params` payload — this is the path
    that works even when the conversation is outside the 24h reply window
    (`can_reply: false`), which is the whole point of templates. Role-gated
    only — see get_messages' docstring on why reference-doc access must be
    bound by the caller (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    if not template_name:
        frappe.throw("template_name is required")
    if isinstance(processed_params, str):
        processed_params = frappe.parse_json(processed_params) if processed_params else {}
    processed_params = processed_params or {}

    conversation_id = frappe.utils.cint(conversation_id)
    result = cw.send_template_message(
        conversation_id,
        name=template_name,
        category=category,
        language=language,
        processed_params=processed_params,
    )
    return result


@frappe.whitelist()
def clear_chatwoot_cache():
    """Admin escape hatch — manually invalidate the read cache without
    waiting out the TTL. System Manager only (frappe.whitelist default
    guest=False already requires a logged-in session; this adds the role gate)."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not permitted", frappe.PermissionError)
    cw.clear_cache()
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
def webhook():
    """Receive Chatwoot webhook events and push a realtime signal to the CRM.

    This is the INSTANT replacement for the 60-second poll in
    realtime_bridge.poll_and_broadcast — Chatwoot's own documented external
    integration primitive. Chatwoot POSTs `message_created` (and related)
    events here the moment they happen; we invalidate the read cache for the
    affected conversation and fire the SAME `chatwoot_message` realtime event
    the poll used, so the CRM frontend refetches immediately instead of waiting
    up to a minute for the next poll tick.

    Guest-open (Chatwoot calls it unauthenticated), so it is gated by a shared
    secret: Chatwoot's Automation/webhook can't send custom auth headers, so the
    token is carried as a `?token=` query param on the URL registered in
    Chatwoot, and compared here in constant time. If no webhook_token is
    configured the check is skipped (poll still covers correctness) but logged.
    """
    settings = frappe.get_cached_doc("Chatwoot Settings") if frappe.db.exists(
        "DocType", "Chatwoot Settings"
    ) else None
    if not settings or not settings.enabled:
        return {"ok": False, "reason": "disabled"}

    if not _webhook_authentic(settings):
        frappe.local.response["http_status_code"] = 401
        return {"ok": False, "reason": "invalid token"}

    try:
        data = frappe.request.get_json(silent=True) or frappe.local.form_dict or {}
    except Exception:
        data = {}

    event = data.get("event")
    # Only conversation/message activity needs to nudge the UI. Chatwoot also
    # sends webhook_verification / contact events we can ignore.
    if event not in (
        "message_created",
        "message_updated",
        "conversation_created",
        "conversation_updated",
        "conversation_status_changed",
    ):
        return {"ok": True, "ignored": event}

    # Chatwoot nests the conversation under `conversation` for message events,
    # or the top-level object IS the conversation for conversation events.
    conversation = data.get("conversation") or data
    # `id` here is Chatwoot's display_id (the human/sequential conversation id),
    # which is exactly what the CRM uses as conversation_id everywhere else.
    conv_id = conversation.get("id") or conversation.get("display_id")
    inbox_id = conversation.get("inbox_id") or data.get("inbox", {}).get("id")

    if conv_id is None:
        return {"ok": True, "no_conversation": True}

    # Drop the cached message page for this conversation so the frontend's
    # immediate refetch gets fresh data instead of a stale cache hit.
    try:
        cw.clear_cache()
    except Exception:
        pass

    frappe.publish_realtime(
        "chatwoot_message",
        {
            "conversation_id": conv_id,
            "inbox_id": inbox_id,
            "updated_at": conversation.get("updated_at") or conversation.get("last_activity_at"),
        },
    )
    return {"ok": True, "event": event, "conversation_id": conv_id}


def _webhook_authentic(settings) -> bool:
    """Constant-time shared-secret check for the guest-open webhook."""
    import hmac

    token = (settings.get("webhook_token") or "").strip() if settings.meta.has_field(
        "webhook_token"
    ) else ""
    if not token:
        frappe.log_error(
            "Chatwoot webhook received but no webhook_token is configured — "
            "signal accepted without verification. Set Chatwoot Settings > "
            "Webhook Token and append ?token=<it> to the Chatwoot webhook URL.",
            "frappe_chatwoot: unverified webhook",
        )
        return True
    provided = frappe.form_dict.get("token") or frappe.get_request_header("X-Chatwoot-Token") or ""
    return hmac.compare_digest(str(provided), str(token))
