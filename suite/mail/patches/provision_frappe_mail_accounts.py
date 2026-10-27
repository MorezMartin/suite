import frappe

from suite.mail.frappe_mail_provisioning import ensure_frappe_mail_account


def execute() -> None:
	"""Provision outgoing ``Frappe Mail`` Email Accounts for users already using Suite mail.

	Users who configured their User Settings before this feature get the same
	zero-configuration outgoing setup new users get via the User Settings hook:
	their own mailbox usable in ERPNext's From picker without per-user setup.
	"""

	for settings in frappe.get_all("User Settings", filters={"username": ("is", "set")}, pluck="user"):
		if frappe.db.exists("User Settings", {"user": settings}):
			try:
				ensure_frappe_mail_account(settings)
			except Exception:
				frappe.log_error(
					title="Failed to provision Frappe Mail Email Account",
					message=f"User: {settings}",
				)
