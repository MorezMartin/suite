"""SSO OTP synchronization between Frappe (source of truth) and Stalwart.

The Frappe 2FA state (per-role ``Role.two_factor_auth`` in Frappe v16, with the
user's encrypted OTP secret) is mirrored into the Stalwart directory so a user
who completed 2FA setup in Frappe is asked for a TOTP code when authenticating
directly against Stalwart (webmail, IMAP with OAuth, etc.).

Unlike password sync (which rides on Suite Cloud, see ``suite.mail.directory``),
credentials OTP are not proxied by Suite Cloud: this module talks to the
Stalwart JMAP administration API (``urn:stalwart:jmap``) directly, using a
dedicated technical account configured in Mail Settings.

Stalwart technical account (documented requirement, outside this code):
    Create e.g. ``sso-sync@<domain>`` carrying the built-in ``User`` role plus
    one minimal custom role with exactly ``sysAccountGet``, ``sysAccountQuery``
    and ``sysAccountUpdate`` — nothing else. Deliberately NOT ``Tenant
    Administrator``: pushing an OTP onto an Admin account would require it, and
    granting tenant-wide write on privileged accounts for a convenience sync is
    a wider blast radius than the feature is worth. Privileged accounts (Admin
    / Tenant / System Administrator roles on Stalwart) are therefore **skipped**
    with a log line: someone holding admin rights completes the Frappe 2FA
    setup and enters the TOTP secret on Stalwart themselves (one QR, same
    secret both sides). JMAP calls here deliberately omit ``accountId`` so they
    run with the technical account's own (tenant-scoped) identity.

Write discipline (validated against the live server before this design):
    - ``otpAuth`` is a **plain string** holding the ``otpauth://`` provisioning
      URL (live-validated: an object payload is rejected ``invalidPatch``, the
      URL string is accepted). Reads mask it — and ``secret`` — with the
      literal string ``"****"``; writing a re-read credential object back
      verbatim would store ``****`` as both. Therefore we NEVER write back the
      credential object: each update patches exactly one nested field,
      ``credentials/<idx>/otpAuth``, and never touches ``secret``. Setting it
      back to ``null`` cleanly removes TOTP (live-validated rollback).
    - Stalwart does not validate the OTP URL on write: a malformed secret
      silently locks the user out of TOTP logins. The secret is only ever
      taken from Frappe's validated OTP secret *after* the user completed the
      live 2FA setup flow (a code was verified), and presence — never the
      value — is re-checked after every write.
    - Every failure is logged (Error Log, module "Mail") and never raised to
      the user: Frappe remains the source of truth and the weekly audit
      re-syncs whatever drifted.
"""

from typing import Any

import frappe
from frappe import _

TOTP_ISSUER = "MH"
STALWART_CAPABILITY = "urn:stalwart:jmap"
JMAP_CORE_CAPABILITY = "urn:ietf:params:jmap:core"


def get_sso_admin_config() -> dict[str, str] | None:
    """Return ``{server, email, secret}`` when direct SSO admin is configured.

    Returns ``None`` — silently, no exception — when Mail Settings has no SSO
    admin configured or no JMAP server url: every entry point then becomes a
    no-op so sites without a direct Stalwart admin keep the previous behavior.
    """

    settings = frappe.get_cached_doc("Mail Settings")
    email = (settings.sso_admin_email or "").strip()
    secret = settings.get_password("sso_admin_secret", raise_exception=False)
    server = (settings.server_url or "").strip()
    if not (email and secret and server):
        return None
    server = server.rstrip("/")
    if not server.endswith("/jmap"):
        server = server + "/jmap"
    return {"server": server, "email": email, "secret": secret}


def get_user_totp_secret(user: str) -> str | None:
    """The user's Base32 TOTP secret, or None when 2FA setup is not completed.

    Frappe v16 stores the secret encrypted in ``DefaultValue`` under the key
    ``<user>_otpsecret`` (decrypted with the per-user key ``<user>.otpsecret``),
    generated lazily during the 2FA setup flow and kept after the user verifies
    a live code. Older Frappe kept it in the (encrypted) ``User.totp_secret``
    field — read as a fallback.
    """

    # Frappe v16 stores the secret under the ``__2fa`` parent, through
    # ``frappe.twofactor.get_default``/``set_default`` — read it back with the very
    # same helper so the key, the parent and the encryption stay symmetric with
    # the writer. ``frappe.defaults`` has no ``get_default`` (ImportError).
    from frappe.twofactor import get_default

    if secret := get_default(f"{user}_otpsecret"):
        secret = secret.strip()
        # v16 ``get_default`` hands back the plaintext base32 (decryption happens
        # inside frappe.db.get_default). Older 2FA data may still be a Fernet
        # token: decrypt only that shape, never an already-decoded secret.
        if secret.startswith("gAAAA"):
            from frappe.utils.password import decrypt

            try:
                return decrypt(secret, key=f"{user}.otpsecret")
            except Exception:
                return None
        return secret or None

    from frappe.utils.password import get_decrypted_password

    return get_decrypted_password("User", user, fieldname="totp_secret", raise_exception=False)


class StalwartAdminError(Exception):
    pass


def _jmap_call(config: dict[str, str], method: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST one JMAP method call to the Stalwart admin endpoint and return its response."""

    import httpx

    payload = {
        "using": [JMAP_CORE_CAPABILITY, STALWART_CAPABILITY],
        "methodCalls": [[method, params, "c1"]],
    }
    try:
        response = httpx.post(
            config["server"],
            json=payload,
            auth=(config["email"], config["secret"]),
            timeout=15,
            verify=False,  # noqa: S501 — matches suite.mail JMAP client behavior (verify_ssl off by default here)
        )
    except httpx.HTTPError as exc:
        raise StalwartAdminError(_("Could not reach the mail server: {0}").format(exc)) from exc

    if response.status_code >= 400:
        raise StalwartAdminError(_("Mail admin API returned HTTP {0}.").format(response.status_code))

    body = response.json()
    responses = body.get("methodResponses") or [[]]
    name, result = responses[0][0], responses[0][1]
    if not name.endswith("/get") and not name.endswith("/set"):
        raise StalwartAdminError(_("Mail admin API error: {0}").format(str(result)[:200]))
    if method.endswith("/set") and (result.get("notFound") or result.get("notCreated") or result.get("notUpdated")):
        failed = result.get("notUpdated") or result.get("notCreated") or result.get("notFound")
        raise StalwartAdminError(_("Mail admin API rejected the update: {0}").format(str(failed)[:200]))
    return result


def find_stalwart_account(config: dict[str, str], email: str) -> dict[str, Any] | None:
    """Return the Stalwart Account object for ``email`` (matched on local part).

    Stalwart stores account ``name`` as the local part (``mm`` for
    ``mm@mhtraiteur.fr``); the full address lives on the principal. Reading
    every account is acceptable at self-hosted scale and keeps the technical
    role to a single ``sysAccountGet`` for lookups.
    """

    local_part = email.split("@", 1)[0].lower()
    result = _jmap_call(config, "x:Account/get", {"ids": None})
    for account in result.get("list", []):
        if isinstance(account, dict) and (account.get("name") or "").lower() == local_part:
            return account
    return None


def _password_credential_index(account: dict[str, Any]) -> str | None:
    """Index (string key) of the account's Password credential, from a masked read."""

    credentials = account.get("credentials") or {}
    for idx, credential in credentials.items():
        if isinstance(credential, dict) and credential.get("@type") == "Password":
            return idx
    return None


def _has_otp(account: dict[str, Any]) -> bool:
    """Whether any credential of the account carries a real otpAuth (read is masked)."""

    for credential in (account.get("credentials") or {}).values():
        if not isinstance(credential, dict):
            continue
        otp = credential.get("otpAuth")
        if otp and otp != "****" and not (isinstance(otp, dict) and otp.get("@type") == "Disabled"):
            return True
        if otp == "****":
            return True
    return False


PRIVILEGED_ROLE_TYPES = frozenset({"Admin", "System Administrator", "Tenant Administrator"})


def _is_privileged(account: dict[str, Any]) -> bool:
    """Whether the Stalwart account carries a built-in admin role type.

    This skip is the ONLY guard: live-probed, a technical account holding
    sysAccountUpdate can patch ANY account's credential (including an Admin's
    otpAuth) — Stalwart does not enforce an ownership check on that path. The
    privileged boundary is therefore code-enforced, not server-enforced; do
    not remove it while granting the technical account sysAccountUpdate.
    Custom roles that merely reference an administrative role through role ids
    are not sniffed (the technical role cannot read role objects).
    """

    roles = account.get("roles") or {}
    return str(roles.get("@type") or "") in PRIVILEGED_ROLE_TYPES


def build_otpauth_url(email: str, secret_b32: str) -> str:
    """Build the otpauth:// provisioning URL pushed to Stalwart."""

    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(TOTP_ISSUER)}:{quote(email, safe='')}?"
        f"secret={secret_b32}&issuer={quote(TOTP_ISSUER)}&algorithm=SHA1&digits=6&period=30"
    )


def sync_otp_for_user(user: str, enable: bool) -> None:
    """Push (or remove) the TOTP credential of the user's Stalwart account.

    No-ops silently when SSO admin is not configured, the user has no mail
    account, or — on enable — Frappe holds no ``totp_secret`` (the user has
    not completed the live 2FA setup yet).
    """

    config = get_sso_admin_config()
    if not config:
        return

    from suite.mail.utils.user import get_account_email

    email = get_account_email(user)
    if not email:
        return

    secret_b32 = None
    if enable:
        secret_b32 = get_user_totp_secret(user)
        if not secret_b32:
            frappe.log_error(
                title="SSO OTP sync skipped: no OTP secret yet",
                message=f"User {user} has 2FA on but Frappe holds no OTP secret yet (setup not completed); "
                "Stalwart left untouched.",
            )
            return

    account = find_stalwart_account(config, email)
    if not account:
        frappe.log_error(
            title="SSO OTP sync skipped: no Stalwart account",
            message=f"No Stalwart account found for {email} (user {user}).",
        )
        return

    if _is_privileged(account):
        frappe.log_error(
            title="SSO OTP sync skipped: privileged Stalwart account",
            message=f"Stalwart account for {email} holds an administrative role; the sync "
            "deliberately never writes privileged credentials. Complete 2FA on Stalwart manually.",
        )
        return

    idx = _password_credential_index(account)
    if idx is None:
        frappe.log_error(
            title="SSO OTP sync skipped: no Password credential",
            message=f"Stalwart account for {email} has no Password credential to attach TOTP to.",
        )
        return

    if enable:
        # otpAuth is a plain otpauth:// URL string (live-validated: object payloads are
        # rejected invalidPatch). Never write the credential object back: reads mask both
        # secret and otpAuth as "****".
        patch = {f"credentials/{idx}/otpAuth": build_otpauth_url(email, secret_b32)}
    else:
        patch = {f"credentials/{idx}/otpAuth": None}

    account_id = account.get("id")
    _jmap_call(config, "x:Account/set", {"update": {account_id: patch}})

    # Re-verify presence (value is masked server-side; presence is all we can — and need — check).
    after = find_stalwart_account(config, email)
    now_has = bool(after and _has_otp(after))
    if enable and not now_has:
        raise StalwartAdminError(_("TOTP push accepted but not present after write for {0}.").format(email))
    if not enable and now_has:
        raise StalwartAdminError(_("TOTP removal accepted but still present after write for {0}.").format(email))


def sync_user_otp(doc, method: str | None = None) -> None:
    """``User`` on_update hook: mirror the 2FA status to Stalwart on transitions.

    Frappe v16 decides 2FA per role (``Role.two_factor_auth``); "enabled" is
    computed from the saved role set, and the 0→1 / 1→0 transition is caught by
    comparing against the last value we synced (kept in Redis, per site — no
    extra doctype). A user whose role set makes 2FA newly on gets their TOTP
    pushed; turning it off removes it on Stalwart (password login stays).
    """

    from frappe.twofactor import two_factor_is_enabled_for_
    from suite.utils import execute_with_logging

    if doc.flags.in_insert:
        return

    user = doc.name
    enable = bool(two_factor_is_enabled_for_(doc))

    cache = frappe.cache()
    key = f"sso_otp_state|{user}"
    synced = cache.get_value(key)
    if synced is None:
        # First observation of this user (e.g. after a Redis flush): never act,
        # only record — the weekly audit repairs anything that had drifted.
        cache.set_value(key, int(enable))
        return
    if bool(synced) == enable:
        return

    cache.set_value(key, int(enable))
    execute_with_logging(
        lambda: sync_otp_for_user(user, enable),
        title="Failed to synchronize the mail account TOTP",
        with_context=False,
        module="Mail",
    )


def audit_otp_drift() -> list[str]:
    """Weekly check: every enabled user with ``enable_2fa`` and a mail account must
    have TOTP present on Stalwart; drifting users are re-synced and reported."""

    config = get_sso_admin_config()
    if not config:
        return []

    two_factor_roles = frappe.get_all("Role", filters={"two_factor_auth": 1}, pluck="name")
    if not two_factor_roles:
        return []
    rows = frappe.db.sql_list(
        """
        select distinct ur.parent
        from `tabHas Role` ur
        inner join `tabUser` u on u.name = ur.parent and u.enabled = 1
        where ur.role in %s
        """,
        (two_factor_roles,),
    )

    drift: list[str] = []
    from suite.mail.utils.user import get_account_email

    for user in rows:
        email = get_account_email(user)
        if not email:
            continue
        try:
            account = find_stalwart_account(config, email)
            if account is None or not _has_otp(account):
                drift.append(user)
                sync_otp_for_user(user, enable=True)
        except Exception:
            frappe.log_error(title=f"SSO OTP audit failed for {user}")

    if drift:
        frappe.log_error(
            title="SSO OTP audit: drift corrected",
            message="Re-synced TOTP for: " + ", ".join(drift),
        )
    return drift
