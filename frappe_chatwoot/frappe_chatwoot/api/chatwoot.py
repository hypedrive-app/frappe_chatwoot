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

    return {
        "meta": raw.get("meta") or {},
        "messages": messages,
    }


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
def get_canned_responses() -> list[dict]:
    """Role-gated only, same as send_message — canned responses are an
    account-level list, not bound to any specific reference doc."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    return cw.list_canned_responses()


@frappe.whitelist()
def clear_chatwoot_cache():
    """Admin escape hatch — manually invalidate the read cache without
    waiting out the TTL. System Manager only (frappe.whitelist default
    guest=False already requires a logged-in session; this adds the role gate)."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not permitted", frappe.PermissionError)
    cw.clear_cache()
    return {"ok": True}
