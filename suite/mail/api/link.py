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
from frappe.utils import cint, cstr, now, sanitize_html

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
        # Subject is mandatory on Communication (v16) and mail without one is legal: core's
        # IMAP pipeline stores "(no subject)" for those, so the bridge mirrors that.
        "subject": getattr(doc, "subject", None) or _("(no subject)"),
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


# --------------------------------------------------------------------------- link panel (v2)

STATUS_FIELDNAMES = ("status", "workflow_status", "document_status")
PREVIEW_FIELDTYPES = {"Data", "Small Text", "Text", "Long Text", "Text Editor", "Int", "Float", "Currency"}
PREVIEW_SKIP_FIELDS = {"name", "subject", "description", "content"}
# A link is display-only data: cheap columns only. Large Text/Text Editor cells make Mail
# Message fetches balloon (some doctypes store megabytes in a single row).
PREVIEW_SELECT_LIMIT = 8


def _status_field(doctype: str) -> str | None:
    meta = frappe.get_meta(doctype)
    for fieldname in STATUS_FIELDNAMES:
        if meta.has_field(fieldname):
            return fieldname
    return None


def _linked_documents_view(communication) -> list[dict]:
    """Communication timeline links + its reference, merged, previews attached.

    The reference (``reference_doctype``/``reference_name`` — set automatically by the
    In-Reply-To machinery or at creation) is prepended when the timeline has no row for it
    yet, as ``source: "auto"``. Timeline rows are ``source: "manual"`` (the Link-to-Documents
    action writes them). Readable-only: a link the user cannot read does not show, and never
    says why.
    """

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    if communication.reference_doctype and communication.reference_name:
        reference = (communication.reference_doctype, communication.reference_name)
        if frappe.db.exists(reference[0], reference[1]) and frappe.has_permission(
            reference[0], ptype="read", doc=reference[1]
        ):
            seen.add(reference)
            rows.append(_link_entry(reference[0], reference[1], None, "auto"))

    for link in communication.timeline_links:
        key = (link.link_doctype, link.link_name)
        if key in seen:
            continue
        if not frappe.has_permission(link.link_doctype, ptype="read", doc=link.link_name):
            continue
        seen.add(key)
        rows.append(_link_entry(link.link_doctype, link.link_name, link.link_title, "manual"))

    for row in rows:
        _attach_preview(row)
    return rows


def _link_entry(doctype: str, name: str, title: str | None, source: str) -> dict:
    meta = frappe.get_meta(doctype)
    display = (title or "").strip()
    if not display:
        title_field = meta.get_title_field()
        display = (
            frappe.db.get_value(doctype, name, title_field, order_by=None, cache=True) if title_field else None
        )
    return {
        "doctype": doctype,
        "name": name,
        "title": display or name,
        "doctype_label": meta.get("label") or doctype,
        "source": source,
    }


def _attach_preview(row: dict) -> None:
    """Cheap display fields for the panel card: status + first short non-empty fields.

    Deliberately narrow (typed subset, short values, bounded count) — never a get_doc on a
    document the mail client has no reason to fully load.
    """

    doctype, name = row["doctype"], row["name"]
    meta = frappe.get_meta(doctype)
    select, status_fieldname = [], _status_field(doctype)
    if status_fieldname:
        select.append(status_fieldname)
    for f in meta.fields:
        if (
            f.fieldname not in PREVIEW_SKIP_FIELDS
            and f.fieldname != status_fieldname
            and f.fieldtype in PREVIEW_FIELDTYPES
            and not f.hidden
            and f.no_copy != 1
            and (f.search_index or f.in_list_view)
        ):
            select.append(f.fieldname)
        if len(select) >= PREVIEW_SELECT_LIMIT:
            break
    if not select:
        return

    values = frappe.db.get_value(doctype, name, select, order_by=None, as_dict=True) or {}
    if status_fieldname:
        status = values.pop(status_fieldname, None)
        if status:
            row["status"] = cstr(status)

    preview = []
    for fieldname, value in values.items():
        if value in (None, "", 0):
            continue
        label = meta.get_label(fieldname)
        preview.append({"label": label, "value": cstr(value)[:80]})
        if len(preview) >= 3:
            break
    if preview:
        row["preview"] = preview


@frappe.whitelist()
def get_message_linked_documents(name: str) -> list[dict]:
    """The panel's payload: documents linked to a Mail Message (manual + automatic)."""

    doc = frappe.get_doc("Mail Message", name)
    get_user_for_jmap_account(doc.account, allow_system_manager=False, raise_exception=True)

    communication = get_communication_for_message(doc)
    if not communication:
        return []
    return _linked_documents_view(communication)


@frappe.whitelist()
def unlink_document(name: str, doctype: str, docname: str | None = None) -> list[dict]:
    """Removes one link between a Mail Message and a document.

    Accepts the panel's naming (``doctype``/``name``) and core's (``doctype``/``docname``).
    Deleting a timeline row lifts the link everywhere it surfaced; when the deleted row was
    the last one for what the Communication also references, the reference fields are
    cleared too — the unlink must not re-surface the document through the reference.
    Unlinking a reference with no timeline row (pure auto-link) just clears the reference.
    A link to the Communication itself (a mail pointed at a mail) is refused: it is the
    thread, not a link.
    """

    if not docname:
        frappe.throw(_("A document name is required."))

    doc = frappe.get_doc("Mail Message", name)
    get_user_for_jmap_account(doc.account, allow_system_manager=False, raise_exception=True)

    communication = get_communication_for_message(doc)
    if not communication:
        frappe.throw(_("This message has no linked documents."))

    if doctype == "Communication":
        frappe.throw(_("A mail cannot be unlinked from the mail it threads to."))

    # Mirror the linking gate: only a user who can read the target may unlink it — dropping
    # a row would otherwise edit a timeline the caller cannot see.
    frappe.has_permission(doctype, ptype="read", doc=docname, throw=True)

    targets = [l for l in communication.timeline_links if l.link_doctype == doctype and l.link_name == docname]
    was_reference = (communication.reference_doctype, communication.reference_name) == (doctype, docname)
    if not targets and not was_reference:
        frappe.throw(_("This message is not linked to {0} {1}.").format(frappe.bold(doctype), frappe.bold(docname)))

    for l in targets:
        communication.timeline_links.remove(l)
    if was_reference:
        communication.reference_doctype = None
        communication.reference_name = None

    communication.save(ignore_permissions=True)
    return _linked_documents_view(communication)


@frappe.whitelist()
def search_linkable_doctypes(txt: str, limit: int = 15) -> list[dict]:
    """DocTypes the user may link a mail to, for the Link-to-Documents combobox.

    Mirrors what the Desk awesomebar can see: readable standalone doctypes with a title.
    ``search_link`` (documents within a doctype) keeps its own permission path — this only
    fills the doctype step the modal currently leaves free-text.
    """

    txt = (txt or "").strip().lower()
    if not txt:
        return []

    allowed = [
        doctype
        for doctype in frappe.get_all("DocType", {"istable": 0, "track_views": ("!=", 1)}, pluck="name")
        if frappe.has_permission(doctype, "read")
        and not doctype.startswith("DocType ")
        and doctype not in {"Communication", "Communication Link", "Email Queue", "Email Queue Recipient"}
    ]

    matches = []
    for doctype in allowed:
        meta = frappe.get_meta(doctype)
        label = (meta.get("label") or doctype).lower()
        if txt in doctype.lower() or txt in label:
            matches.append({"doctype": doctype, "label": meta.get("label") or doctype})
        if len(matches) >= cint(limit):
            break
    return matches


def auto_link_incoming_message(doc) -> str | None:
    """Links a newly-ingested incoming message to the document the mail it answers belongs to.

    Default-on and silent. Never raises — mail ingestion must not break because linking failed.
    """

    try:
        if doc.draft or doc.junk:
            return None

        if not doc.message_id or not doc.in_reply_to:
            return None

        existing = get_communication_for_message(doc)
        if existing:
            # Idempotent re-run: report the Communication this mail already holds.
            return existing.name

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
