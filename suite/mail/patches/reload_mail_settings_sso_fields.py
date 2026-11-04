import frappe


def execute():
    """Reload Mail Settings so the SSO admin fields exist before first use."""

    frappe.reload_doc("Mail", "doctype", "Mail Settings")
