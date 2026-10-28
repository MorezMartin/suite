# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Bridges Suite mail (JMAP) and Frappe Communications so mail can be tied to documents.

Suite mail lives in its own world: Mail Messages live on the JMAP server and never touch
``Communication`` on their own — so none of the core's linking machinery (Timeline Links, the
document timeline, In-Reply-To inheritance) could ever see Suite mail. This module builds that
bridge on top of what core already provides: ``Communication.add_link`` writes ``Timeline Link``
rows, so a mail links to any number of documents exactly the way core links mail to records.

Two entry points:

``link_to_documents`` (manual): the whitelisted API behind the mail reader's "Link to Documents"
action. It resolves (creating when needed) the ``Communication`` for a Mail Message and appends
the requested document links to it.

``auto_link_new_messages`` (automatic, default-on): called from the JMAP change-sync for each
batch of new incoming messages. When a message's In-Reply-To matches a mail the site sent — an
``Email Queue`` or ``Communication`` message-id — the new ``Communication`` inherits the
parent's ``reference_doctype``/``reference_name``, mirroring
``frappe.email.receive.InboundMail.parent_*()``/``reference_document()``. No setting, no toggle.

The ingestion path must never break on linking: auto-linking is best-effort and logged.
"""

import json
import re
from typing import TypedDict

import frappe
from frappe import _
from frappe.utils import now, sanitize_html

from suite.mail.doctype.user_account.user_account import get_user_for_jmap_account
from suite.mail.utils import log_mail_error
from suite.mail.utils.dt import from_utc_z
from suite.mail.utils.user import get_account_emails


class DocumentLink(TypedDict):
    """One document to link a mail to, with an optional human-readable title."""

    doctype: str
    name: str
    title: str | None


def split_message_id(value: str | None) -> str | None:
    """Returns the bare message-id, angle brackets and whitespace stripped.

    JMAP hands ``messageId``/``inReplyTo`` bracketed (``<abc@example.com>``); core stores them
    bare (``receive.py`` keeps only what sits between the brackets), so both worlds key on the
    same shape.
    """

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


def is_reply_to_system_sent_mail(in_reply_to: str | None) -> bool:
    """Whether the referenced message-id was minted by this site.

    Same semantics as ``InboundMail.is_reply_to_system_sent_mail`` in frappe/email/receive.py:
    core's ``get_message_id()`` builds message-ids as ``<uuid@site>``, so the site substring is
    the marker that the replied mail left this site (and an Email Queue row can exist for it).
    """

    return bool(
        in_reply_to and frappe.local.site and frappe.local.site.lower() in in_reply_to.lower()
    )


def find_parent_email_queue(in_reply_to: str | None):
    """The Email Queue the reply answers, when the replied mail was sent by this site."""

    if not is_reply_to_system_sent_mail(in_reply_to):
        return None

    from frappe.email.doctype.email_queue.email_queue import EmailQueue

    return EmailQueue.find_one_by_filters(message_id=in_reply_to)


def find_parent_communication(in_reply_to: str | None):
    """The Communication a reply answers, looked up by message-id (core ``parent_communication``)."""

    from frappe.core.doctype.communication.communication import Communication

    if not in_reply_to:
        return None

    parent = Communication.find_one_by_filters(message_id=in_reply_to, order_by="creation DESC")
    if parent:
        return parent

    # Sometimes the Communication name itself is the message-id's local part; core falls back
    # to that lookup and so do we.
    reference = in_reply_to.partition("@")[0]
    return Communication.find(reference, ignore_error=True) if reference else None


def find_reference_document(parent_communication, parent_email_queue):
    """The (doctype, name) the reply inherits, from the parent records (core ``reference_document()``).

    Precedence mirrors the core: Email Queue first, then the parent Communication. Unlike core
    there is no ``append_to``/subject matching — this bridge fires only on a real In-Reply-To
    match, where the reference is carried by the parent, never guessed from the subject.
    """

    parent = None
    if parent_email_queue and parent_email_queue.reference_doctype:
        parent = parent_email_queue
    elif parent_communication and parent_communication.reference_doctype:
        parent = parent_communication

    if not parent:
        return None

    if not frappe.db.exists(parent.reference_doctype, parent.reference_name):
        return None

    return parent.reference_doctype, parent.reference_name


def get_communication_for_message(doc):
    """The Communication that already represents this message, if any (dedup by message-id)."""

    message_id = split_message_id(doc.message_id)
    if not message_id:
        return None

    name = frappe.db.get_value(
        "Communication", {"message_id": message_id}, "name", order_by="creation DESC"
    )
    return frappe.get_doc("Communication", name) if name else None


def build_communication_values(doc, *, user: str | None = None) -> dict:
    """Communication field values mirroring a received/sent copy of a Mail Message.

    ``doc`` is a (virtual) Mail Message: only fields the formatted JMAP dict is known to carry
    are read, with fallbacks for a freshly imported message that may lack them.
    """

    message_id = split_message_id(doc.message_id)
    is_sent = getattr(doc, "email_type", "Received") == "Sent"

    def joined(recipients):
        # The virtual doctype's to/cc/bcc properties yield {"name", "email"}; the formatted
        # JMAP dict yields {"display_name", "email"} — accept both shapes.
        return ", ".join(
            f'"{name}" <{r["email"]}>' if (name := r.get("display_name") or r.get("name")) else r["email"]
            for r in recipients or []
            if r.get("email")
        )

    sender = getattr(doc, "from_email", None)
    if getattr(doc, "from_name", None) and sender:
        sender = f'"{doc.from_name}" <{sender}>'

    content = getattr(doc, "html_body", None) or getattr(doc, "text_body", None) or ""
    if getattr(doc, "html_body", None):
        # The JMAP body is raw network input: sanitize like receive.py sanitizes incoming HTML.
        content = sanitize_html(doc.html_body)

    raw_date = getattr(doc, "sent_at", None) if is_sent else getattr(doc, "received_at", None)
    communication_date = from_utc_z(raw_date) or raw_date or now()

    return {
        "doctype": "Communication",
        "communication_type": "Communication",
        "communication_medium": "Email",
        "sent_or_received": "Sent" if is_sent else "Received",
        "message_id": message_id,
        # in_reply_to is a Link to Communication: it is filled with the parent Communication's
        # name by the caller, never with a raw message-id.
        "subject": getattr(doc, "subject", None),
        "sender": sender,
        "recipients": joined(getattr(doc, "to", None)) or None,
        "cc": joined(getattr(doc, "cc", None)) or None,
        "bcc": joined(getattr(doc, "bcc", None)) or None,
        "content": content,
        "communication_date": communication_date,
        # Point the timeline at the mailbox owner, the way core points it at the mail account
        # user; the syncing user is the one acting on this mail.
        "user": user or frappe.session.user,
        # The mail is already delivered and read in the mail client. The Communication exists so
        # the core's linking and timeline machinery apply — not to raise a second notification.
        "seen": 1,
        "unread_notification_sent": 1,
    }

def create_communication_for_message(doc, *, reference=None, parent_communication=None, user=None):
    """Inserts the Communication mirroring ``doc``, in receive-mode like core's pipeline.

    ``reference`` is an inherited ``(doctype, name)`` tuple; setting it at insert time makes core
    mark the Communication ``Linked`` and scope its read permission to that document from birth.
    """

    values = build_communication_values(doc, user=user)

    if parent_communication:
        values["in_reply_to"] = parent_communication.name
    if reference:
        values["reference_doctype"], values["reference_name"] = reference

    communication = frappe.get_doc(values)
    # Receive-mode: skip the outbound recipient validation and the contact auto-linking core's
    # IMAP pipeline also skips for incoming mail — a link must never depend on contact data.
    communication.flags.in_receive = True
    communication.insert(ignore_permissions=True)
    return communication


def get_or_create_communication(doc, *, auto: bool = False, fallback_reference=None):
    """The Communication of a Mail Message: reused if it exists, created otherwise.

    With ``auto`` (the ingestion path) nothing is created unless the message's In-Reply-To
    matches a mail this site sent or a Communication it already holds — that match is the only
    automatic trigger; stray mail gets no Communication. ``fallback_reference`` (a
    ``(doctype, name)`` tuple) applies only on creation, when nothing was inherited.
    """

    if communication := get_communication_for_message(doc):
        return communication

    in_reply_to = split_message_id(getattr(doc, "in_reply_to", None))
    parent_email_queue = find_parent_email_queue(in_reply_to)
    parent_communication = find_parent_communication(in_reply_to)
    reference = find_reference_document(parent_communication, parent_email_queue)

    if auto and not (parent_email_queue or parent_communication):
        return None

    if reference is None and parent_communication and is_reply_to_system_sent_mail(in_reply_to):
        # A reply on top of a site-sent Communication that carries no reference itself: core
        # falls back to the parent Communication as the reference (receive.py, last resort).
        reference = ("Communication", parent_communication.name)

    if reference is None:
        reference = fallback_reference

    user = get_user_for_jmap_account(doc.account, ignore_permissions=True)
    return create_communication_for_message(
        doc, reference=reference, parent_communication=parent_communication, user=user
    )


def validate_document_link_target(doctype: str, name: str) -> None:
    """Throws unless the session user may link to this document.

    Security is explicit here, not delegated: linking publishes the mail's content on the
    target document's timeline, so only a user who can read the target may link to it.
    """

    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("DocType {0} does not exist.").format(frappe.bold(doctype)))

    if frappe.get_meta(doctype).istable:
        frappe.throw(
            _("Cannot link a mail against a child document: {0}").format(frappe.bold(doctype))
        )

    # Permission before existence: a user who cannot read the document should not learn whether
    # it exists.
    frappe.has_permission(doctype, ptype="read", doc=name, throw=True)

    if not frappe.db.exists(doctype, name):
        frappe.throw(_("Document {0} {1} does not exist.").format(frappe.bold(doctype), frappe.bold(name)))


def _validated_links(links: list) -> list[DocumentLink]:
    """Validates each requested link's shape, then its target's existence and readability.

    A link is ``{doctype, name}`` or equivalently ``{reference_doctype, reference_name}`` (the
    core's naming for a Communication reference), with an optional display ``title``.
    """

    validated = []
    for link in links:
        if not isinstance(link, dict):
            frappe.throw(_("Each link needs a doctype and a document name."))

        doctype = (link.get("doctype") or link.get("reference_doctype") or "").strip()
        name = (link.get("name") or link.get("reference_name") or "").strip()
        if not doctype or not name:
            frappe.throw(_("Each link needs a doctype and a document name."))

        validate_document_link_target(doctype, name)
        validated.append(
            {
                "doctype": doctype,
                "name": name,
                "title": (link.get("title") or "").strip() or None,
            }
        )

    return validated


@frappe.whitelist()
def link_to_documents(name: str, links: list | str) -> dict:
    """Links the Communication of a Mail Message to one or more documents (Timeline Links).

    ``links`` is a list of ``{doctype, name, title?}`` (form-encoded JSON strings accepted).
    Permissions, both enforced: the caller must own the JMAP account the message lives in
    (reading a Mail Message is already gated per-account, and linking must not leak another
    user's mail), and must be able to read every target document. Idempotent: re-linking an
    existing pair does not duplicate the row. Returns the communication and its full link list.
    """

    if isinstance(links, str):
        links = json.loads(links)

    if not links:
        frappe.throw(_("At least one document link is required."))

    doc = frappe.get_doc("Mail Message", name)
    if doc.draft:
        frappe.throw(_("Drafts cannot be linked to documents."))

    # Same gate get_messages applies to reading the message at all: its owner (or Administrator).
    get_user_for_jmap_account(doc.account, allow_system_manager=False, raise_exception=True)

    if not split_message_id(doc.message_id):
        frappe.throw(_("Mail Message {0} has no Message-ID and cannot be linked.").format(frappe.bold(name)))

    links = _validated_links(links)

    fallback = None
    if links[0]["doctype"] != "Communication":
        # The first link scopes the Communication's read permission (core has_permission keys on
        # the reference) — mirror what parse_email_for_timeline_links does for automatic linking.
        # Linking a Communication into itself is refused: validate_reference rejects that cycle.
        fallback = (links[0]["doctype"], links[0]["name"])

    communication = get_or_create_communication(doc, fallback_reference=fallback)
    if not communication:
        frappe.throw(_("Could not create the Communication for this message."))

    existing = {(l.link_doctype, l.link_name) for l in communication.timeline_links}
    titles = {}
    for link in links:
        if (link["doctype"], link["name"]) in existing:
            continue
        communication.add_link(link["doctype"], link["name"])
        if link["title"]:
            titles[(link["doctype"], link["name"])] = link["title"]

    communication.save(ignore_permissions=True)

    # Core's validate() deduplicates timeline links and recomputes their titles from each
    # doctype's title field, so requested titles land on the rows after the save, not before.
    for row in communication.timeline_links:
        if title := titles.get((row.link_doctype, row.link_name)):
            frappe.db.set_value("Communication Link", row.name, "link_title", title, update_modified=False)
            row.link_title = title

    return {
        "communication": communication.name,
        "links": [
            {"doctype": l.link_doctype, "name": l.link_name, "title": l.link_title}
            for l in communication.timeline_links
        ],
    }


@frappe.whitelist()
def get_message_document_links(name: str) -> list[dict]:
    """The documents this message is linked to (its Communication's readable Timeline Links)."""

    doc = frappe.get_doc("Mail Message", name)
    get_user_for_jmap_account(doc.account, allow_system_manager=False, raise_exception=True)

    communication = get_communication_for_message(doc)
    if not communication:
        return []

    return [
        {"doctype": l.link_doctype, "name": l.link_name, "title": l.link_title}
        for l in communication.timeline_links
        if frappe.has_permission(l.link_doctype, ptype="read", doc=l.link_name)
    ]


def auto_link_incoming_message(doc) -> str | None:
    """Links a newly-ingested incoming message to the document the mail it answers belongs to.

    Default-on and silent. Never raises — mail ingestion must not break because linking failed.
    """

    try:
        if doc.draft or doc.junk:
            return None

        if not doc.message_id or not doc.in_reply_to:
            return None

        if get_communication_for_message(doc):
            return None

        in_reply_to = split_message_id(doc.in_reply_to)
        # Cheap gates first: the In-Reply-To must point at this site's mail (core semantics) or
        # at a Communication the site already holds; only incoming mail gets a bridge of its own
        # — the account's own Sent copies already sit behind whatever sent them. email_type
        # reads the account's identities, so it runs last, only for mail that may match.
        if not is_reply_to_system_sent_mail(in_reply_to) and not find_parent_communication(in_reply_to):
            return None

        if getattr(doc, "email_type", "Received") != "Received":
            return None

        communication = get_or_create_communication(doc, auto=True)
        return communication.name if communication else None

    except Exception:
        log_mail_error(_("Failed to link mail to documents"), frappe.get_traceback(with_context=True))
        return None


def auto_link_new_messages(account: str, messages: list[dict]) -> None:
    """Best-effort auto-linking for a batch of new JMAP messages (formatted dicts from the sync).

    Cheap header checks run on the already-fetched dicts; the (virtual) Mail Message — whose load
    re-reads the message over JMAP — is only fetched for messages that could still match: a
    reply from someone else, not a draft, not junk.
    """

    own_addresses = {email.lower() for email in get_account_emails(account)}

    for message in messages:
        try:
            if not message.get("id") or not message.get("in_reply_to"):
                continue
            if message.get("draft") or message.get("junk"):
                continue
            # A reply from the account's own owner is a sent copy, not an incoming reply.
            if (message.get("from_email") or "").lower() in own_addresses:
                continue

            from suite.mail.doctype.mail_message.mail_message import get_messages

            loaded = get_messages(account, [message["id"]])
            if not loaded:
                continue
            auto_link_incoming_message(frappe.get_doc("Mail Message", loaded[0]["name"]))
        except Exception:
            log_mail_error(_("Failed to link mail to documents"), frappe.get_traceback(with_context=True))
