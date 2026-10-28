# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Linking Suite mail to documents: the message-id matching that mirrors frappe.email.receive,
plus the "Link to Documents" API and the auto-linking that fires when an incoming reply's
In-Reply-To matches a mail this site sent."""

import unittest
import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from suite.mail.api.link import (
    auto_link_incoming_message,
    find_parent_documents,
    is_site_message_id,
    link_to_documents,
    referenced_message_ids,
    split_message_id,
)


class TestMessageIdMatching(UnitTestCase):
    """Header parsing and site-ownership checks — pure logic, no documents involved."""

    def test_angle_brackets_are_stripped(self):
        self.assertEqual(split_message_id("<abc@example.com>"), "abc@example.com")
        self.assertEqual(split_message_id(" abc@example.com "), "abc@example.com")
        self.assertIsNone(split_message_id("<>"))
        self.assertIsNone(split_message_id(None))

    def test_in_reply_to_and_references_are_parsed_in_order(self):
        message_ids = referenced_message_ids(
            "<reply@site.test>",
            "<root@site.test> <mid@elsewhere.test>",
        )
        self.assertEqual(message_ids, ["reply@site.test", "root@site.test", "mid@elsewhere.test"])

    def test_duplicate_ids_are_kept_once(self):
        message_ids = referenced_message_ids("<a@site.test>", "<a@site.test> <b@site.test>")
        self.assertEqual(message_ids, ["a@site.test", "b@site.test"])

    def test_noise_around_bracketed_ids_is_ignored(self):
        # A sloppy "Re: <id>" header keeps only what sits inside the brackets.
        self.assertEqual(referenced_message_ids("Re: hello <id@site.test>"), ["id@site.test"])

    def test_site_ownership_matches_the_core_check(self):
        # frappe.email.receive gates on `frappe.local.site in in_reply_to`; so do we.
        self.assertTrue(is_site_message_id(f"abc@{frappe.local.site}"))
        self.assertFalse(is_site_message_id("abc@not-this-site.test"))
        self.assertFalse(is_site_message_id(None))


class FakeMailMessage:
    """The slice of a (virtual) Mail Message that the linking bridge reads.

    Mail Messages live on the JMAP server and cannot be created in the test database, so the
    bridge is driven with this stub — which also keeps the double-Communication question visible:
    the bridge keys everything on message_id, the same shape core stores, tested below.
    """

    def __init__(self, **overrides):
        self.name = "acct|msg-1"
        self.account = "acct"
        self.message_id = f"mail-{frappe.generate_hash()}@elsewhere.test"
        self.in_reply_to = None
        self.subject = "Re: a conversation"
        self.text_body = "Body"
        self.from_email = "outer@example.test"
        self.to = [{"email": "me@example.test"}]
        self.cc = []
        self.received_at = "2026-01-01 00:00:00"
        self.sent_at = "2026-01-01 00:00:00"
        self.seen = 0
        self.draft = 0
        self.junk = 0
        self.email_type = "Received"
        self.parsed_message = None

        for field, value in overrides.items():
            setattr(self, field, value)

    def check_permission(self, *args, **kwargs):
        pass


class TestMailDocumentLinking(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.todo = frappe.get_doc({"doctype": "ToDo", "description": "Mail linking target"}).insert()
        cls.contact = frappe.get_doc({"doctype": "Contact", "first_name": "Link Target"}).insert()

    def _stub_mail_message_get(self, doc):
        """Patches frappe.get_doc so ("Mail Message", name) resolves to the stub."""

        real_get_doc = frappe.get_doc

        def fake_get_doc(*args, **kwargs):
            if args and args[0] == "Mail Message":
                return doc
            return real_get_doc(*args, **kwargs)

        return unittest.mock.patch.object(frappe, "get_doc", fake_get_doc)

    def test_link_to_documents_creates_communication_and_links(self):
        doc = FakeMailMessage()

        with self._stub_mail_message_get(doc):
            result = link_to_documents(
                doc.name,
                [
                    {"doctype": "ToDo", "name": self.todo.name},
                    {"doctype": "Contact", "name": self.contact.name, "title": "The big lead"},
                ],
            )

        communication = frappe.get_doc("Communication", result["communication"])
        self.assertEqual(communication.message_id, doc.message_id)
        self.assertEqual(communication.sent_or_received, "Received")

        linked = {(l.link_doctype, l.link_name) for l in communication.timeline_links}
        self.assertIn(("ToDo", self.todo.name), linked)
        self.assertIn(("Contact", self.contact.name), linked)
        titled = {(l.link_doctype, l.link_name): l.link_title for l in communication.timeline_links}
        self.assertEqual(titled[("Contact", self.contact.name)], "The big lead")

        # Idempotent: linking the same document again does not duplicate the row.
        with self._stub_mail_message_get(doc):
            again = link_to_documents(doc.name, [{"doctype": "ToDo", "name": self.todo.name}])

        self.assertEqual(
            len(frappe.get_all("Communication Link", {"parent": again["communication"], "link_doctype": "ToDo"})),
            1,
        )

    def test_matching_in_reply_to_inherits_reference(self):
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

    def test_non_matching_in_reply_to_is_a_no_op(self):
        # Foreign message-id: no site in it, so it is skipped before any lookup (core semantics).
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

    def test_find_parent_documents_prefers_email_queue(self):
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

        reference, parent_communication = find_parent_documents([message_id])
        # Same precedence as InboundMail.reference_document(): Email Queue first.
        self.assertEqual(reference["reference_doctype"], "ToDo")
        self.assertEqual(parent_communication["reference_doctype"], "Contact")
