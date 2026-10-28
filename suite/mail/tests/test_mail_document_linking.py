# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Linking Suite mail to documents: the message-id matching that mirrors frappe.email.receive,
the "Link to Documents" API, and the auto-linking that fires when an incoming reply's
In-Reply-To matches a mail this site sent."""

import json
import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from suite.mail.api.link import (
    auto_link_incoming_message,
    find_parent_communication,
    find_parent_email_queue,
    is_reply_to_system_sent_mail,
    link_to_documents,
    referenced_message_ids,
    split_message_id,
    validate_document_link_target,
)


class TestMessageIdMatching(UnitTestCase):
    """Header parsing and site-ownership checks — pure logic, no documents involved."""

    def test_angle_brackets_are_stripped(self):
        self.assertEqual(split_message_id("<abc@example.com>"), "abc@example.com")
        self.assertEqual(split_message_id(" abc@example.com "), "abc@example.com")
        self.assertIsNone(split_message_id("<>"))
        self.assertIsNone(split_message_id(None))
        self.assertIsNone(split_message_id(""))

    def test_in_reply_to_and_references_are_parsed_in_order(self):
        message_ids = referenced_message_ids(
            "<reply@site.test>",
            "<root@site.test> <mid@elsewhere.test>",
        )
        self.assertEqual(message_ids, ["reply@site.test", "root@site.test", "mid@elsewhere.test"])

    def test_noise_around_bracketed_ids_is_ignored(self):
        # A sloppy "Re: <id>" header keeps only what sits inside the brackets.
        self.assertEqual(referenced_message_ids("Re: hello <id@site.test>"), ["id@site.test"])

    def test_site_ownership_matches_the_core_check(self):
        # frappe.email.receive gates on `frappe.local.site in in_reply_to`; so do we.
        self.assertTrue(is_reply_to_system_sent_mail(f"abc@{frappe.local.site}"))
        self.assertFalse(is_reply_to_system_sent_mail("abc@not-this-site.test"))
        self.assertFalse(is_reply_to_system_sent_mail(None))


class FakeMailMessage:
    """The slice of a (virtual) Mail Message that the linking bridge reads.

    Mail Messages live on the JMAP server and cannot be created in the test database, so the
    bridge is driven with this stub — which also keeps the message-id keying (the same shape
    core stores) visible in every test below.
    """

    def __init__(self, **overrides):
        self.name = "acct|msg-1"
        self.account = "acct"
        self.message_id = f"mail-{frappe.generate_hash()}@elsewhere.test"
        self.in_reply_to = None
        self.subject = "Re: a conversation"
        self.html_body = None
        self.text_body = "Body"
        self.from_email = "outer@example.test"
        self.from_name = "Outer Sender"
        self.to = [{"name": "Me", "email": "me@example.test"}]
        self.cc = []
        self.bcc = []
        self.received_at = "2026-01-01T00:00:00Z"
        self.sent_at = "2026-01-01T00:00:00Z"
        self.draft = 0
        self.junk = 0
        self.email_type = "Received"

        for field, value in overrides.items():
            setattr(self, field, value)


class TestMailDocumentLinking(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.todo = frappe.get_doc({"doctype": "ToDo", "description": "Mail linking target"}).insert()
        cls.contact = frappe.get_doc({"doctype": "Contact", "first_name": "Link Target"}).insert()

    # --- helpers --------------------------------------------------------------

    def _bridge_patches(self, doc):
        """Patch frappe.get_doc for ("Mail Message", name) and the account-ownership gate:
        the stub's ``acct`` is not a JMAP Account row, and account gating itself is covered
        by the mail app's own tests — here the Communication bridge is what's under test."""

        real_get_doc = frappe.get_doc

        def fake_get_doc(*args, **kwargs):
            if args and args[0] == "Mail Message":
                return doc
            return real_get_doc(*args, **kwargs)

        return (
            unittest.mock.patch.object(frappe, "get_doc", fake_get_doc),
            unittest.mock.patch(
                "suite.mail.api.link.get_user_for_jmap_account", lambda *a, **kw: "me@example.test"
            ),
        )

    def _link(self, doc, links):
        get_doc_patch, account_patch = self._bridge_patches(doc)
        with get_doc_patch, account_patch:
            return link_to_documents(doc.name, links)

    # --- link_to_documents (manual) -------------------------------------------

    def test_link_to_documents_creates_communication_and_links(self):
        doc = FakeMailMessage()

        result = self._link(
            doc,
            [
                {"doctype": "ToDo", "name": self.todo.name},
                {"doctype": "Contact", "name": self.contact.name, "title": "The big lead"},
            ],
        )

        communication = frappe.get_doc("Communication", result["communication"])
        self.assertEqual(communication.message_id, doc.message_id)
        self.assertEqual(communication.sent_or_received, "Received")
        self.assertEqual(communication.reference_doctype, "ToDo")
        self.assertEqual(communication.reference_name, self.todo.name)

        linked = {(l.link_doctype, l.link_name) for l in communication.timeline_links}
        self.assertIn(("ToDo", self.todo.name), linked)
        self.assertIn(("Contact", self.contact.name), linked)
        titled = {(l.link_doctype, l.link_name): l.link_title for l in communication.timeline_links}
        self.assertEqual(titled[("Contact", self.contact.name)], "The big lead")

    def test_link_to_documents_accepts_json_string_and_reference_naming(self):
        doc = FakeMailMessage()

        result = self._link(
            doc, json.dumps([{"reference_doctype": "ToDo", "reference_name": self.todo.name}])
        )

        linked = {(l["doctype"], l["name"]) for l in result["links"]}
        self.assertEqual(linked, {("ToDo", self.todo.name)})

    def test_link_to_documents_is_idempotent(self):
        doc = FakeMailMessage()

        self._link(doc, [{"doctype": "ToDo", "name": self.todo.name}])
        again = self._link(doc, [{"doctype": "ToDo", "name": self.todo.name}])

        rows = frappe.get_all(
            "Communication Link", {"parent": again["communication"], "link_doctype": "ToDo"}
        )
        self.assertEqual(len(rows), 1)

    def test_link_to_documents_reuses_existing_communication(self):
        doc = FakeMailMessage()
        sent = frappe.get_doc(
            {
                "doctype": "Communication",
                "communication_type": "Communication",
                "communication_medium": "Email",
                "sent_or_received": "Sent",
                "sender": "me@example.test",
                "recipients": "outer@example.test",
                "subject": "a conversation",
                "message_id": doc.message_id,
            }
        ).insert()

        result = self._link(doc, [{"doctype": "ToDo", "name": self.todo.name}])

        self.assertEqual(result["communication"], sent.name)
        self.assertEqual(len(frappe.get_all("Communication", {"message_id": doc.message_id})), 1)

    def test_link_to_documents_denies_unreadable_target(self):
        # A regular user cannot read Email Queue: linking to one must be refused, and nothing
        # may be created for the message.
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"link-denied-{frappe.generate_hash()}@example.test",
                "first_name": "Denied",
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
        queue = frappe.get_doc(
            {
                "doctype": "Email Queue",
                "sender": "site@example.test",
                "recipients": [{"recipient": "outer@example.test", "status": "Sent"}],
            }
        ).insert(ignore_permissions=True)

        doc = FakeMailMessage()
        with self.set_user(user.email), self.assertRaises(frappe.PermissionError):
            self._link(doc, [{"doctype": "Email Queue", "name": queue.name}])

        self.assertFalse(frappe.db.exists("Communication", {"message_id": doc.message_id}))

    def test_validate_document_link_target_rules(self):
        # Unknown doctype and missing document are refused; a readable one passes (Administrator).
        with self.assertRaises(frappe.ValidationError):
            validate_document_link_target("No Such DocType", "x")
        with self.assertRaises(frappe.ValidationError):
            validate_document_link_target("ToDo", "SOMETHING-0000-99999")
        validate_document_link_target("ToDo", self.todo.name)

    def test_draft_cannot_be_linked(self):
        doc = FakeMailMessage(draft=1)
        with self.assertRaises(frappe.ValidationError):
            self._link(doc, [{"doctype": "ToDo", "name": self.todo.name}])

    # --- automatic linking at ingestion ----------------------------------------

    def test_matching_parent_communication_inherits_reference(self):
        parent = frappe.get_doc(
            {
                "doctype": "Communication",
                "communication_type": "Communication",
                "communication_medium": "Email",
                "sent_or_received": "Sent",
                "sender": "me@example.test",
                "recipients": "outer@example.test",
                "subject": "a conversation",
                "message_id": f"sent-{frappe.generate_hash()}@{frappe.local.site}",
                "reference_doctype": "ToDo",
                "reference_name": self.todo.name,
            }
        ).insert()

        reply = FakeMailMessage(
            message_id=f"reply-{frappe.generate_hash()}@elsewhere.test",
            in_reply_to=f"<{parent.message_id}>",
        )
        communication_name = auto_link_incoming_message(reply)

        self.assertIsNotNone(communication_name)
        communication = frappe.get_doc("Communication", communication_name)
        self.assertEqual(communication.reference_doctype, "ToDo")
        self.assertEqual(communication.reference_name, self.todo.name)
        self.assertEqual(communication.sent_or_received, "Received")
        self.assertEqual(communication.in_reply_to, parent.name)

        # Re-running the hook for the same mail reuses the Communication instead of duplicating.
        self.assertEqual(auto_link_incoming_message(reply), communication_name)
        self.assertEqual(len(frappe.get_all("Communication", {"message_id": reply.message_id})), 1)

    def test_matching_email_queue_inherits_reference(self):
        message_id = f"queued-{frappe.generate_hash()}@{frappe.local.site}"
        frappe.get_doc(
            {
                "doctype": "Email Queue",
                "message_id": message_id,
                "reference_doctype": "Contact",
                "reference_name": self.contact.name,
                "recipients": [{"recipient": "outer@example.test", "status": "Sent"}],
            }
        ).insert(ignore_permissions=True)

        reply = FakeMailMessage(
            message_id=f"reply-{frappe.generate_hash()}@elsewhere.test",
            in_reply_to=f"<{message_id}>",
        )
        communication_name = auto_link_incoming_message(reply)

        self.assertIsNotNone(communication_name)
        communication = frappe.get_doc("Communication", communication_name)
        self.assertEqual(communication.reference_doctype, "Contact")
        self.assertEqual(communication.reference_name, self.contact.name)

    def test_email_queue_reference_takes_precedence_over_communication(self):
        # Same precedence as InboundMail.reference_document(): Email Queue first.
        message_id = f"both-{frappe.generate_hash()}@{frappe.local.site}"
        frappe.get_doc(
            {
                "doctype": "Email Queue",
                "message_id": message_id,
                "reference_doctype": "ToDo",
                "reference_name": self.todo.name,
                "recipients": [{"recipient": "outer@example.test", "status": "Sent"}],
            }
        ).insert(ignore_permissions=True)
        frappe.get_doc(
            {
                "doctype": "Communication",
                "communication_type": "Communication",
                "communication_medium": "Email",
                "sent_or_received": "Sent",
                "sender": "me@example.test",
                "message_id": message_id,
                "reference_doctype": "Contact",
                "reference_name": self.contact.name,
            }
        ).insert()

        queue = find_parent_email_queue(message_id)
        found = find_parent_communication(message_id)
        self.assertEqual(queue.reference_doctype, "ToDo")
        self.assertEqual(found.reference_doctype, "Contact")

        reply = FakeMailMessage(
            message_id=f"reply-{frappe.generate_hash()}@elsewhere.test",
            in_reply_to=f"<{message_id}>",
        )
        communication_name = auto_link_incoming_message(reply)
        communication = frappe.get_doc("Communication", communication_name)
        self.assertEqual(communication.reference_doctype, "ToDo")

    def test_non_matching_in_reply_to_is_a_no_op(self):
        # Foreign message-id: no site in it and no Communication match, so nothing is created.
        reply = FakeMailMessage(
            message_id=f"reply-{frappe.generate_hash()}@elsewhere.test",
            in_reply_to="<foreign-mail@not-a-site.test>",
        )
        self.assertIsNone(auto_link_incoming_message(reply))
        self.assertFalse(frappe.db.exists("Communication", {"message_id": reply.message_id}))

        # A site-shaped message-id that matches no Email Queue/Communication is a no-op too.
        unmatched = FakeMailMessage(
            message_id=f"reply-{frappe.generate_hash()}@elsewhere.test",
            in_reply_to=f"<ghost@{frappe.local.site}>",
        )
        self.assertIsNone(auto_link_incoming_message(unmatched))
        self.assertFalse(frappe.db.exists("Communication", {"message_id": unmatched.message_id}))

    def test_plain_incoming_mail_is_a_no_op(self):
        incoming = FakeMailMessage(in_reply_to=None)
        self.assertIsNone(auto_link_incoming_message(incoming))
        self.assertFalse(frappe.db.exists("Communication", {"message_id": incoming.message_id}))

    def test_sent_and_junk_replies_are_no_ops(self):
        junk = FakeMailMessage(in_reply_to=f"<whatever@{frappe.local.site}>", junk=1)
        self.assertIsNone(auto_link_incoming_message(junk))

        sent_copy = FakeMailMessage(
            in_reply_to=f"<whatever@{frappe.local.site}>",
            email_type="Sent",
        )
        self.assertIsNone(auto_link_incoming_message(sent_copy))
