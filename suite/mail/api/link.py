# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Bridges Suite mail (JMAP) and Frappe Communications so mail can be tied to documents.

Suite mail lives in its own world: Mail Messages live on the JMAP server and never touch
``Communication`` on their own. This module builds that bridge on top of what core already
provides — ``Communication.add_link`` writes ``Timeline Link`` rows, so a mail can be linked
to any number of documents exactly the way core links mail threads to records.

Two entry points:

``link_to_documents`` (manual): a whitelisted API for the mail reader's "Link to Documents"
action. It resolves (creating when needed) the ``Communication`` for a Mail Message and appends
the requested document links to it.

``auto_link_incoming_message`` (automatic): called from the JMAP sync loop whenever a new
incoming message arrives. Mirroring the ``In-Reply-To`` inheritance of ``frappe.email.receive``
(kept to the same matching semantics), a reply to a mail this site sent — recognized by the
site domain inside the referenced message-id, the same check core does — inherits the
``reference_doctype``/``reference_name`` of the matching ``Email Queue`` or ``Communication``.
The mail ingestion path never breaks on linking errors: everything here is best-effort and
logged.
"""

import json
import re
from typing import TypedDict

import frappe
from frappe import _

from suite.mail.utils import log_mail_error


class DocumentLink(TypedDict):
    """One document to link a mail to, with an optional human-readable title."""

    doctype: str
    name: str
    title: str | None


def split_message_id(value: str | None) -> str | None:
    """Returns the bare message-id, angle brackets stripped — same as core's get_string_between."""

    if not value:
        return None

    value = value.strip()
    if value.startswith("<"):
        value = value[1:]
    if value.endswith(">"):
        value = value[:-1]

    return value or None


def referenced_message_ids(in_reply_to: str | None, references: str | None = None) -> list[str]:
    """Returns the referenced message-ids to probe: In-Reply-To first, then References.

    ``References`` is a space-separated list of bracketed message-ids (RFC 5322); ids are pulled
    out of angle brackets, falling back to bare whitespace-separated tokens for sloppy senders.
    """

    message_ids = []
    for value in [in_reply_to, references]:
        if not value or not isinstance(value, str):
            continue
        # A bracketed header is parsed for its brackets alone: bare words around them ("Re: <id>")
        # are noise, not message-ids. Bare token lists only apply to senders who drop the brackets.
        pattern = r"<([^>]+)>" if "<" in value else r"([^\s]+)"
        for match in re.finditer(pattern, value):
            if mid := split_message_id(match.group(1)):
                if mid not in message_ids:
                    message_ids.append(mid)

    return message_ids


def is_site_message_id(message_id: str | None) -> bool:
    """Whether the message-id was minted by this site.

    Same semantics as ``InboundMail.is_reply_to_system_sent_mail`` in frappe/email/receive.py:
    core's ``get_message_id()`` builds message-ids as ``<uuid@site>`` (and sometimes embeds a
    Communication name), so a site-domain substring means the referenced mail left this site.
    """

    return bool(message_id and frappe.local.site in message_id)


def find_parent_documents(message_ids: list[str]) -> tuple:
    """Returns (reference_parent, parent_communication) inherited from the mail being replied to.

    Probes each site-owned message-id against Email Queue and Communication message-ids, the way
    core's ``InboundMail.parent_email_queue``/``parent_communication`` do; returns empty values
    when no referenced message-id belongs to this site.
    """

    parent_email_queue = ""
    parent_communication = ""
    for message_id in message_ids:
        if not is_site_message_id(message_id):
            continue

        if not parent_email_queue:
            parent_email_queue = frappe.db.get_value(
                "Email Queue",
                {"message_id": message_id},
                ["name", "reference_doctype", "reference_name"],
                as_dict=True,
                order_by="creation desc",
            )

        if not parent_communication:
            found = frappe.get_all(
                "Communication",
                {"message_id": message_id},
                ["name", "reference_doctype", "reference_name"],
                as_dict=True,
                order_by="creation desc",
                limit=1,
            )
            parent_communication = found[0] if found else ""

        if parent_email_queue or parent_communication:
            break

    def has_reference(doc) -> bool:
        return bool(doc and doc.get("reference_doctype") and doc.get("reference_name"))

    # Inheritance order mirrors InboundMail.reference_document(): Email Queue wins, then Communication.
    reference = parent_email_queue if has_reference(parent_email_queue) else parent_communication
    return reference or "", parent_communication


def get_or_create_communication(doc, reference: dict | None = None) -> str:
    """Returns the name of the Communication for this Mail Message, creating it when absent.

    Mail Messages are virtual documents over JMAP: they carry no Communication of their own.
    Communications are keyed on the mail's Message-ID (bare, the same shape core stores), so
    both this bridge and core's IMAP path reuse one document per mail instead of duplicating.
    ``reference`` optionally carries the reference_doctype/reference_name to inherit.
    """

    communication_name = frappe.db.get_value(
        "Communication",
        {"message_id": doc.message_id},
        "name",
        order_by="creation desc",
    )
    if communication_name:
        return communication_name

    recipients = ", ".join(r["email"] for r in doc.to + doc.cc if r.get("email"))
    in_reply_to = split_message_id(doc.in_reply_to)

    communication = frappe.get_doc(
        {
            "doctype": "Communication",
            "communication_type": "Communication",
            "communication_medium": "Email",
            "sent_or_received": doc.email_type,
            "sender": doc.from_email,
            "recipients": recipients or None,
            "subject": doc.subject,
            "text_content": doc.text_body,
            "communication_date": doc.received_at or doc.sent_at,
            "message_id": doc.message_id,
            "in_reply_to": in_reply_to,
            "seen": doc.seen,
        }
    )

    if reference:
        communication.reference_doctype = reference.get("reference_doctype")
        communication.reference_name = reference.get("reference_name")

    # Point the Timeline at the mailbox owner, the way core points it at the mail account user.
    if account_user := frappe.db.get_value("User Account", {"account": doc.account}, "user"):
        communication.user = account_user

    communication.insert(ignore_permissions=True)
    return communication.name


def _validated_links(links: list) -> list[DocumentLink]:
    """Validates each requested link: the doctype exists, the document exists, the user can read it."""

    validated = []
    for link in links:
        doctype = (link.get("doctype") or "").strip()
        name = (link.get("name") or "").strip()
        if not doctype or not name:
            frappe.throw(_("Each link needs a doctype and a document name."))

        if not frappe.db.exists("DocType", doctype):
            frappe.throw(_("DocType {0} does not exist.").format(frappe.bold(doctype)))

        if not frappe.db.exists(doctype, name):
            frappe.throw(_("Document {0} {1} does not exist.").format(frappe.bold(doctype), frappe.bold(name)))

        if not frappe.has_permission(doctype, doc=name, ptype="read"):
            frappe.throw(
                _("You do not have permission to link to {0} {1}.").format(frappe.bold(doctype), frappe.bold(name))
            )

        validated.append({"doctype": doctype, "name": name, "title": (link.get("title") or "").strip() or None})

    return validated


@frappe.whitelist()
def link_to_documents(name: str, links: list) -> dict:
    """Links the Communication of a Mail Message to one or more documents (Timeline Links).

    ``links`` is a list of ``{doctype, name, title?}``. Permissions: reading the message already
    means owning the mailbox (the virtual Mail Message load gates on it), the target documents
    must be readable, and the Communication must be creatable. Idempotent: re-linking an
    existing pair does not duplicate the row. Returns the communication and its link list.
    """

    if isinstance(links, str):
        links = json.loads(links)

    doc = frappe.get_doc("Mail Message", name)
    doc.check_permission("read")

    if not doc.message_id:
        frappe.throw(_("Mail Message {0} has no Message-ID and cannot be linked.").format(frappe.bold(name)))

    links = _validated_links(links)

    if not frappe.has_permission("Communication", ptype="create"):
        frappe.throw(_("You do not have permission to link mail to documents."))

    communication = frappe.get_doc("Communication", get_or_create_communication(doc))
    existing = {(l.link_doctype, l.link_name) for l in communication.timeline_links}

    titles = {}
    for link in links:
        if (link["doctype"], link["name"]) in existing:
            continue

        communication.add_link(link["doctype"], link["name"])
        if link["title"]:
            titles[(link["doctype"], link["name"])] = link["title"]

    # Core's validate() deduplicates timeline links and recomputes their titles, so requested
    # titles are written onto the Communication Link rows after the save, not before it.
    communication.save(ignore_permissions=True)
    for row in communication.timeline_links:
        if title := titles.get((row.link_doctype, row.link_name)):
            frappe.db.set_value(
                "Communication Link", row.name, "link_title", title, update_modified=False
            )
            row.link_title = title

    return {
        "communication": communication.name,
        "links": [
            {"doctype": l.link_doctype, "name": l.link_name, "title": l.link_title}
            for l in communication.timeline_links
        ],
    }


def auto_link_incoming_message(doc) -> str | None:
    """Links a newly-ingested incoming Mail Message to the document its mail replies to.

    Default-on and silent: called from the JMAP sync loop after a new message is seen. When the
    In-Reply-To (or References) matches a mail this site sent, the Communication is created (or
    reused) with the parent's reference inherited; otherwise nothing happens. Never raises — mail
    ingestion must not break because linking failed.
    """

    try:
        if doc.draft or doc.junk:
            return None

        if not doc.message_id or not doc.in_reply_to:
            return None

        if frappe.db.exists("Communication", {"message_id": doc.message_id}):
            return None

        reference, parent_communication = find_parent_documents(referenced_message_ids(doc.in_reply_to))
        if not (reference or parent_communication):
            # In-Reply-To was not ours: References may still carry the chain. Probing it costs the
            # blob fetch, so it stays behind the reply-header gate (core matches In-Reply-To alone).
            references = doc.parsed_message.get_header("References") if doc.parsed_message else None
            reference, parent_communication = find_parent_documents(referenced_message_ids(references))
            if not (reference or parent_communication):
                return None

        # Only incoming mail gets a bridge of its own; the account's own Sent copies are already
        # represented by whatever Communication sent the mail. email_type asks JMAP for the
        # account's identities, so it runs last — only for mail that actually matches.
        if getattr(doc, "email_type", "Received") != "Received":
            return None

        return get_or_create_communication(doc, reference=reference or parent_communication)

    except Exception:
        log_mail_error(_("Failed to link mail to documents"), frappe.get_traceback(with_context=True))
        return None


def auto_link_new_messages(account: str, messages: list[dict]) -> None:
    """Best-effort auto-linking for a batch of new JMAP messages (account + formatted message dicts).

    Cheap header checks run on the already-fetched dicts; the (virtual) Mail Message is only
    loaded for messages that could still match — a reply, not a draft, not junk.
    """

    for message in messages:
        if not message.get("id") or not message.get("in_reply_to") or message.get("draft") or message.get("junk"):
            continue

        try:
            doc = frappe.get_doc("Mail Message", f"{account}|{message['id']}")
        except Exception:
            continue

        auto_link_incoming_message(doc)
