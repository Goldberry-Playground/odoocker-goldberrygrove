# -*- coding: utf-8 -*-
"""
Provision the CEO's prod Odoo login HEADLESSLY, inside an Odoo shell (full DB
context, superuser `env`). GOL-1780: Josh (CEO) has NO res.users login on prod
Odoo -- there is no Odoo account credential in any 1Password vault, only API
service keys and the master/DB password. This bootstraps his first login.

    docker compose \
        -f docker-compose.yml \
        -f docker-compose.override.grove.yml \
        -f docker-compose.override.production.yml \
        exec -T odoo odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/provision_ceo_user_shell.py

WHY A SHELL SCRIPT (not XML-RPC)
    Bootstrapping the FIRST admin user is a chicken-and-egg: XML-RPC
    (common.authenticate) needs an existing res.users login + password/API key,
    and none exists for a human on prod. The only admin secret on the droplet is
    the Odoo **master password** (odoo.conf admin_passwd / ODOO_ADMIN_PASSWORD),
    which gates /web/database management -- it is NOT a res.users credential, so
    there is nothing for XML-RPC to authenticate as. Inside `odoo shell`, `env`
    already runs as SUPERUSER, so no credential is needed. This is the same
    mechanism GOL-89 (provision-logistics-otto) uses and the CEO ratified.

WHAT IT DOES (idempotent, re-runnable)
    1. Find-or-create login josh@goldberrygrove.farm with a CEO/admin group set
       (Administration/Settings + Sales/Inventory/Purchase/Website managers).
       Groups are ensured with (4, gid) links -- additive, never clobbers.
    2. Deliver a credential WITHOUT Terra ever handling a plaintext password:
         * Preferred: generate a one-time self-service SET-PASSWORD link via the
           auth_signup module (res.partner.signup_url). Josh opens it and sets
           his OWN password. No email/SMTP dependency, nothing stored anywhere.
         * Fallback (auth_signup not installed): set a strong random password and
           print it ONCE, between markers, to the operator's own terminal. Josh
           logs in, changes it under Preferences, and saves it in 1Password.
       Either way the secret is emitted only to the terminal of whoever runs
       this script -- never to an issue comment, a log artifact, or a store
       Terra controls.

DRY RUN
    Set CEO_DRY_RUN=1 in the container env to print the resolved groups + the
    intended action and the SMTP/auth_signup posture WITHOUT writing anything
    (no create, no commit). Prints NOTHING sensitive.
"""

import os
import secrets
import string
import sys

CEO_LOGIN = "josh@goldberrygrove.farm"
CEO_NAME = "Joshua Dunbar"
CEO_EMAIL = "josh@goldberrygrove.farm"
DRY_RUN = os.environ.get("CEO_DRY_RUN", "").strip() not in ("", "0", "false", "False")
# Delivery channel for the credential:
#   "print" (default) -> emit the one-time set-password URL / temp password to
#     THIS terminal. Use for the manual SSH runbook (private to the operator).
#   "email"           -> call action_reset_password() so Odoo emails Josh the
#     set-password link; print only a non-secret confirmation. Use in CI, whose
#     logs are durable -- nothing secret must land there. Requires working SMTP.
CEO_DELIVER = (os.environ.get("CEO_DELIVER", "print").strip().lower() or "print")

# Full CEO/admin posture. base.group_system (Administration / Settings) is the
# ratified "Settings access" grant GOL-1780 asks about -- for a solo-operator
# CEO it is correct. The app-manager groups make the Apps menu + product/catalog
# editing show up cleanly. Optional groups (app not installed) are a WARN, never
# fatal. base.group_user (Internal User) + base.group_system are REQUIRED.
REQUIRED_GROUP_XMLIDS = [
    "base.group_user",     # Internal User (mandatory base for any backend user)
    "base.group_system",   # Administration / Settings (full admin config)
]
OPTIONAL_GROUP_XMLIDS = [
    "base.group_erp_manager",            # Access Rights (implied by group_system, explicit for clarity)
    "sales_team.group_sale_manager",     # Sales / Administrator
    "stock.group_stock_manager",         # Inventory / Administrator
    "purchase.group_purchase_manager",   # Purchase / Administrator
    "account.group_account_manager",     # Accounting (if installed)
    "website.group_website_designer",    # Website: full editor
    "website.group_website_restricted_editor",  # Website: content editor (fallback tier)
]


def _err(*a):
    print("[provision-ceo]", *a, file=sys.stderr, flush=True)


# `env` is injected by `odoo shell`. Fail loudly if run the wrong way.
try:
    env  # noqa: F821  (provided by the shell namespace)
except NameError:
    _err(
        "ERROR: `env` is not defined. Run this INSIDE an Odoo shell, e.g.\n"
        "  docker compose ... exec -T odoo odoo shell -d \"$DB_NAME\" --no-http "
        "< scripts/provision_ceo_user_shell.py"
    )
    raise SystemExit(2)


def _resolve_groups():
    group_ids = []
    missing_required = []
    for xmlid in REQUIRED_GROUP_XMLIDS:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if not rec or rec._name != "res.groups":
            missing_required.append(xmlid)
            _err(f"ERROR: REQUIRED group missing: {xmlid}")
            continue
        group_ids.append(rec.id)
        _err(f"resolved {xmlid} -> res.groups({rec.id})")
    for xmlid in OPTIONAL_GROUP_XMLIDS:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if not rec or rec._name != "res.groups":
            _err(f"WARN: optional group not found (module not installed?): {xmlid}")
            continue
        group_ids.append(rec.id)
        _err(f"resolved {xmlid} -> res.groups({rec.id})")
    return group_ids, missing_required


def _mail_posture():
    """Report whether a self-service link (auth_signup) and/or SMTP are usable.
    Informational only -- the signup-URL path does not require SMTP."""
    signup_installed = bool(
        env["ir.module.module"].search_count(
            [("name", "=", "auth_signup"), ("state", "=", "installed")]
        )
    )
    mail_servers = env["ir.mail_server"].sudo().search_count([])
    cfg_smtp = (env["ir.config_parameter"].sudo().get_param("mail.default.from") or "").strip()
    _err(
        f"posture: auth_signup={'installed' if signup_installed else 'MISSING'}, "
        f"ir.mail_server records={mail_servers}, mail.default.from="
        f"{'set' if cfg_smtp else 'unset'}"
    )
    return signup_installed


def main():
    group_ids, missing_required = _resolve_groups()
    if missing_required:
        _err(f"ABORT: required groups missing {missing_required} -- wrong DB/modules?")
        return 3
    _err(f"resolved {len(group_ids)} groups total: {group_ids}")

    signup_installed = _mail_posture()
    user = env["res.users"].search([("login", "=", CEO_LOGIN)], limit=1)

    if DRY_RUN:
        action = "UPDATE groups on existing" if user else "CREATE"
        print("----BEGIN CEO_PROVISION_DRYRUN----")
        print(f"login={CEO_LOGIN}")
        print(f"name={CEO_NAME}")
        print(f"action={action}{(' (uid=%d)' % user.id) if user else ''}")
        for xmlid in REQUIRED_GROUP_XMLIDS + OPTIONAL_GROUP_XMLIDS:
            rec = env.ref(xmlid, raise_if_not_found=False)
            ok = rec and rec._name == "res.groups"
            tier = "REQUIRED" if xmlid in REQUIRED_GROUP_XMLIDS else "optional"
            print(f"group {xmlid} [{tier}] -> {'granted' if ok else 'SKIP(not-installed)'}")
        print(f"credential_delivery={'auth_signup self-service link' if signup_installed else 'random temp password (auth_signup missing)'}")
        print("No write, no commit.")
        print("----END CEO_PROVISION_DRYRUN----")
        return 0

    if user:
        user.write({
            "name": CEO_NAME,
            "email": CEO_EMAIL,
            "groups_id": [(4, gid) for gid in group_ids],
        })
        _err(f"updated existing user uid={user.id}, ensured CEO/admin groups")
    else:
        user = env["res.users"].create({
            "name": CEO_NAME,
            "login": CEO_LOGIN,
            "email": CEO_EMAIL,
            "groups_id": [(6, 0, group_ids)],
        })
        _err(f"created user uid={user.id}")

    env.cr.commit()
    print(f"CEO_ODOO_UID={user.id}")

    # --- Credential delivery (no plaintext ever handled by Terra) -------------
    if CEO_DELIVER == "email":
        # CI-safe: emit nothing secret to the (durable) log; Odoo emails the
        # set-password link to the CEO. Requires auth_signup + working SMTP.
        if not signup_installed:
            _err("ERROR: CEO_DELIVER=email needs auth_signup installed -- it is not.")
            return 4
        if not hasattr(user, "action_reset_password"):
            _err("ERROR: action_reset_password unavailable (auth_signup?).")
            return 4
        try:
            user.action_reset_password()
            env.cr.commit()
            print(f"CEO_RESET_EMAIL_SENT_TO={CEO_EMAIL}")
            _err(
                "Reset/set-password email dispatched. If it does not arrive, prod "
                "SMTP is not wired -- re-run with the manual SSH path (CEO_DELIVER "
                "unset) to get a one-time link on your own terminal instead."
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            _err(f"ERROR: action_reset_password failed ({exc!r}); check prod SMTP.")
            return 4

    if signup_installed:
        # Prepare a one-time signup/reset token and emit the self-service URL.
        try:
            user.partner_id.signup_prepare(signup_type="reset")
            env.cr.commit()
            url = user.partner_id.signup_url
            base = env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
            _err(f"web.base.url={base or 'UNSET (fix under Settings > Technical > System Parameters)'}")
            print("----BEGIN CEO_SET_PASSWORD_URL----")
            print(url or "(signup_url empty -- check web.base.url / auth_signup)")
            print("----END CEO_SET_PASSWORD_URL----")
            _err(
                "Open the URL above ONCE to set the CEO password. It is a one-time "
                "token -- treat it like a secret; do NOT paste it into any issue."
            )
            return 0
        except Exception as exc:  # noqa: BLE001 - fall back to a temp password
            _err(f"WARN: signup URL path failed ({exc!r}); falling back to temp password")

    # Fallback: strong random password, printed ONCE to this terminal only.
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    pw = "".join(secrets.choice(alphabet) for _ in range(24))
    user.password = pw
    env.cr.commit()
    print("----BEGIN CEO_TEMP_PASSWORD----")
    print(pw)
    print("----END CEO_TEMP_PASSWORD----")
    _err(
        "TEMP password set. Log in at /web/login, change it immediately under "
        "Preferences > Account Security, then store the new value in 1Password "
        "(Grove Prod vault). This value is shown only here, only once."
    )
    return 0


raise SystemExit(main())
