import frappe
from frappe.utils.password import get_decrypted_password


def get_frappe_mail_site() -> str:
	"""The site Frappe Mail Email Accounts should point at — this site itself."""

	return frappe.utils.get_url()


def ensure_frappe_mail_account(user: str) -> str | None:
	"""Provision an outgoing ``Frappe Mail`` Email Account for ``user``.

	Called when a user's JMAP access becomes usable (User Settings saved and accounts
	synced), so that ERPNext/core ``find_outgoing(match_by_email)`` picks their own
	mailbox without any per-user configuration.

	Only provisions when:
	  - the user is enabled and has the Suite User role (the Frappe Mail API
	    routes require it), and
	  - one of the user's User Accounts matches the user's own email address —
	    the account the From-picker will list.

	Returns the Email Account name, or None when nothing was provisioned.
	"""

	if frappe.db.get_value("User", user, "enabled") in (0, None):
		return None

	if "Suite User" not in frappe.get_roles(user):
		return None

	settings = frappe.db.get_value("User Settings", {"user": user}, "name")
	if not settings:
		return None

	# The user's own address must be one of their synced mail accounts.
	account = frappe.qb.DocType("User Account")
	mailbox = (
		frappe.qb.from_(account)
		.where(account.user == user)
		.select(account.account)
		.limit(1)
		.run()
	)
	if not mailbox:
		return None

	email_id = user if "@" in user else frappe.db.get_value("User", user, "email")
	if frappe.db.exists("Email Account", {"email_id": email_id}):
		return frappe.db.get_value("Email Account", {"email_id": email_id}, "name")

	api_key = frappe.db.get_value("User", user, "api_key")
	if api_key:
		api_secret = get_decrypted_password("User", user, "api_secret", raise_exception=False)
	else:
		from frappe.core.doctype.user.user import generate_keys

		generate_keys(user)
		api_key = frappe.db.get_value("User", user, "api_key")
		api_secret = get_decrypted_password("User", user, "api_secret", raise_exception=False)

	if not (api_key and api_secret):
		return None

	# Two transactions: inserting an Email Account whose email_id belongs to a User
	# triggers core's setup_user_email_inbox, whose SELECT ... FOR UPDATE on the User
	# row collides with the Frappe Mail validation HTTP loopback when both run in one
	# transaction (MariaDB 1020). Inserting first without the service, then flipping
	# it to Frappe Mail, keeps the two writers apart.
	frappe.db.commit()

	ea = frappe.new_doc("Email Account")
	ea.email_id = email_id
	ea.email_account_name = f"{email_id.split('@')[0]} (Suite)"
	ea.enable_outgoing = 1
	ea.enable_incoming = 0
	ea.awaiting_password = 1  # core requires a password for outgoing accounts; cleared below
	ea.flags.ignore_permissions = True
	ea.insert(ignore_permissions=True)
	frappe.db.commit()

	ea = frappe.get_doc("Email Account", ea.name)
	ea.service = "Frappe Mail"
	ea.frappe_mail_site = get_frappe_mail_site()
	ea.api_key = api_key
	ea.api_secret = api_secret
	ea.awaiting_password = 0
	ea.flags.ignore_permissions = True
	ea.save()
	frappe.db.commit()

	return ea.name
