"""Tests for suite.mail.sso_otp.

Pure-logic tests always run. The end-to-end push test is skipped unless a
dedicated staging pair is configured via site config keys::

    sso_otp_test_admin_email / sso_otp_test_admin_secret
    sso_otp_test_user  (a User with 2FA completed and a Stalwart mailbox)

Those guard against mutating the production mail server during CI.
"""

import unittest

import frappe

from suite.mail.sso_otp import (
    _has_otp,
    _password_credential_index,
    build_otpauth_url,
    get_sso_admin_config,
)


class TestSsoOtpLogic(unittest.TestCase):
    def test_build_otpauth_url(self):
        url = build_otpauth_url("test@mhtraiteur.fr", "JBSWY3DPEHPK3PXP")
        self.assertTrue(url.startswith("otpauth://totp/MH:test%40mhtraiteur.fr?"))
        self.assertIn("secret=JBSWY3DPEHPK3PXP", url)
        self.assertIn("algorithm=SHA1&digits=6&period=30", url)

    def test_password_credential_index(self):
        account = {"credentials": {
            "0": {"@type": "AppPassword"},
            "1": {"@type": "Password", "secret": "****"},
        }}
        self.assertEqual(_password_credential_index(account), "1")
        self.assertIsNone(_password_credential_index({"credentials": {}}))

    def test_has_otp(self):
        with_otp = {"credentials": {"0": {"@type": "Password", "otpAuth": "****"}}}
        without = {"credentials": {"0": {"@type": "Password", "otpAuth": None}}}
        self.assertTrue(_has_otp(with_otp))
        self.assertFalse(_has_otp(without))
        self.assertFalse(_has_otp({"credentials": {}}))


class TestSsoOtpIntegration(unittest.TestCase):
    """End-to-end push/remove against a staging Stalwart, guarded by site config."""

    @classmethod
    def setUpClass(cls):
        conf = frappe.local.conf
        cls.admin_email = conf.get("sso_otp_test_admin_email")
        cls.admin_secret = conf.get("sso_otp_test_admin_secret")
        cls.user = conf.get("sso_otp_test_user")
        if not (cls.admin_email and cls.admin_secret and cls.user):
            raise unittest.SkipTest("set sso_otp_test_* site config keys to run")

    def test_push_and_remove(self):
        from suite.mail import sso_otp

        config = get_sso_admin_config()
        self.assertIsNotNone(config)
        sso_otp.sync_otp_for_user(self.user, enable=True)
        account = sso_otp.find_stalwart_account(config, frappe.db.get_value("User Settings", {"user": self.user}, "username"))
        self.assertTrue(sso_otp._has_otp(account))
        sso_otp.sync_otp_for_user(self.user, enable=False)
        account = sso_otp.find_stalwart_account(config, frappe.db.get_value("User Settings", {"user": self.user}, "username"))
        self.assertFalse(sso_otp._has_otp(account))
