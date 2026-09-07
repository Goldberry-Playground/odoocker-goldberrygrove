# -*- coding: utf-8 -*-
"""
Provision the CEO's prod Odoo login HEADLESSLY, inside an Odoo shell (full DB
context, superuser `env`). GOL-1780: Josh (CEO) has NO res.users login on prod
Odoo -- there is no Odoo account credential in any 1Password vault, only API
service keys and the master/DB password. This bootstraps his first login.

    set -a; . /etc/grove/.env; set +a
    cd /etc/grove && docker compose exec -T odoo \
        odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/provision_ceo_user_shell.py

    (The live prod droplet runs a SINGLE docker-compose.yml under /etc/grove --
    no override files, no /opt/grove. Confirmed off the running container's own
    compose labels, 2026-08-31.)

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
       Groups are ensured with (4, gid) links -- additive, never clobbers. If the
       found user is a hand-made PORTAL/share account (Odoo's Portal and Public
       roles are mutually exclusive with Internal), base.group_portal /
       base.group_public are DROPPED in the SAME write that adds base.group_user,
       or `_check_disjoint_groups` aborts the promotion.
    1b. Resolve the intended COMPANY set and set BOTH company_id (active) and
       company_ids (allowed), additively (GOL-1842 / GOL-1811 root cause). On a
       multi-company DB a user with no explicit company_ids is left on whatever
       default the DB picks, so the other LLC storefronts are invisible to him --
       exactly the GOL-1811 symptom. company_ids is ensured with (4, cid) links
       (additive, never drops an existing allowed company); company_id is set to
       the resolved active company, guaranteed to be within the allowed set.
       Default intended set = ALL companies in the DB (correct for a solo-operator
       CEO who owns every entity); override with CEO_COMPANIES / CEO_MAIN_COMPANY.
    2. Deliver a credential WITHOUT Terra ever handling a plaintext password:
         * Self-service link (auth_signup, res.partner.signup_url): taken ONLY
           when that attribute exists. Odoo 19 removed signup_url, so this path
           is skipped on prod today and the script falls through to:
         * Direct temp password: set a strong random password and print it ONCE,
           between markers, to the operator's own terminal. Josh logs in, changes
           it under Preferences, and saves it in 1Password. This is the path that
           actually unblocked the CEO on prod 2026-08-31.
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

# Company scoping (GOL-1842 / GOL-1811). Both optional:
#   CEO_COMPANIES     SEMICOLON-separated res.company NAMES to allow. Empty
#                     (default) => ALL companies in the DB (a solo-operator CEO
#                     owns every entity). ';' not ',' because a company name may
#                     itself contain a comma ("At The Grove Nursery, LLC"). Names
#                     are matched exactly; a name not found WARNs and is skipped
#                     rather than aborting.
#   CEO_MAIN_COMPANY  the res.company NAME to make ACTIVE (company_id). Empty
#                     (default) => keep the user's current active company if it is
#                     inside the allowed set, else the lowest-id allowed company
#                     (the DB's main company). Always forced into the allowed set
#                     so Odoo's "company_id must be in company_ids" holds.
CEO_COMPANIES = os.environ.get("CEO_COMPANIES", "").strip()
CEO_MAIN_COMPANY = os.environ.get("CEO_MAIN_COMPANY", "").strip()

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


def _groups_field():
    """Odoo 19 renamed res.users.groups_id -> group_ids (m2m to res.groups).
    Resolve the field at runtime rather than hardcoding either name, so this
    script survives the version straddle: 17/18 (groups_id) AND 19+ (group_ids).
    Prod is 19.0 as of 2026-08-31 -- writing the old name raises
    ValueError: Invalid field 'groups_id' in 'res.users' before any commit."""
    fields = env["res.users"]._fields
    return "group_ids" if "group_ids" in fields else "groups_id"


def _exclusive_group_removals(user, groups_field):
    """(3, gid) unlink commands for any exclusive Portal/Public group the found
    user currently holds.

    Odoo's `_check_disjoint_groups` forbids an internal user (base.group_user)
    from ALSO being in the mutually-exclusive Portal or Public roles. A
    find-or-create that promotes a hand-made PORTAL/share account therefore MUST
    drop base.group_portal / base.group_public in the SAME write that adds
    base.group_user, or the write raises:

        ValidationError: User '...' cannot be at the same time in exclusive
        groups 'Role / Portal', 'Role / User'.

    Verified live on prod 2026-08-31: uid=7 (josh@) was a hand-made portal signup
    (share=True, its only group base.group_portal) and could only be promoted to
    internal by dropping group_portal in the same write. Returns [] for a user
    that is already internal (the common re-run case)."""
    removals = []
    current_ids = getattr(user, groups_field).ids
    for xmlid in ("base.group_portal", "base.group_public"):
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec and rec._name == "res.groups" and rec.id in current_ids:
            removals.append((3, rec.id))
            _err(
                f"will drop exclusive group {xmlid} -> res.groups({rec.id}) "
                "(portal/internal disjoint constraint)"
            )
    return removals


def _resolve_companies(user):
    """Resolve (allowed_companies, active_company) for the CEO.

    allowed = CEO_COMPANIES names, or ALL companies when unset.
    active  = CEO_MAIN_COMPANY, or the user's current active company if it is in
              the allowed set, else the lowest-id allowed company. The active
              company is always unioned into the allowed set so Odoo's
              company_id-in-company_ids constraint holds.
    Returns (allowed_recordset, active_record). Never returns an empty allowed
    set: a DB always has at least one company.
    """
    Company = env["res.company"].sudo()
    all_companies = Company.search([], order="id")

    if CEO_COMPANIES:
        allowed = Company.browse()
        missing = []
        for name in [n.strip() for n in CEO_COMPANIES.split(";") if n.strip()]:
            rec = Company.search([("name", "=", name)], limit=1)
            if rec:
                allowed |= rec
            else:
                missing.append(name)
        if missing:
            _err(f"WARN: CEO_COMPANIES names not found (skipped): {missing}")
        if not allowed:
            _err("WARN: no CEO_COMPANIES matched any company; using ALL companies")
            allowed = all_companies
    else:
        allowed = all_companies

    # Resolve the active company.
    active = Company.browse()
    if CEO_MAIN_COMPANY:
        active = Company.search([("name", "=", CEO_MAIN_COMPANY)], limit=1)
        if not active:
            _err(f"WARN: CEO_MAIN_COMPANY '{CEO_MAIN_COMPANY}' not found; falling back")
    if not active and user and user.company_id and user.company_id in allowed:
        active = user.company_id
    if not active:
        active = allowed.sorted("id")[0]

    # Guarantee the active company is within the allowed set (constraint).
    allowed |= active
    return allowed, active


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

    groups_field = _groups_field()
    _err(f"res.users groups m2m field resolved -> {groups_field}")

    signup_installed = _mail_posture()
    user = env["res.users"].search([("login", "=", CEO_LOGIN)], limit=1)
    allowed_companies, active_company = _resolve_companies(user)
    _err(
        "resolved companies: allowed="
        + ", ".join(f"{c.name}({c.id})" for c in allowed_companies.sorted("id"))
        + f" | active={active_company.name}({active_company.id})"
    )

    if DRY_RUN:
        action = "UPDATE groups+companies on existing" if user else "CREATE"
        print("----BEGIN CEO_PROVISION_DRYRUN----")
        print(f"login={CEO_LOGIN}")
        print(f"name={CEO_NAME}")
        print(f"action={action}{(' (uid=%d)' % user.id) if user else ''}")
        print(f"groups_m2m_field={groups_field}")
        for xmlid in REQUIRED_GROUP_XMLIDS + OPTIONAL_GROUP_XMLIDS:
            rec = env.ref(xmlid, raise_if_not_found=False)
            ok = rec and rec._name == "res.groups"
            tier = "REQUIRED" if xmlid in REQUIRED_GROUP_XMLIDS else "optional"
            print(f"group {xmlid} [{tier}] -> {'granted' if ok else 'SKIP(not-installed)'}")
        cur = (
            ", ".join(f"{c.name}({c.id})" for c in user.company_ids.sorted("id"))
            if user else "(new user)"
        )
        print(f"company_ids current -> {cur}")
        print(
            "company_ids resolved (additive) -> "
            + ", ".join(f"{c.name}({c.id})" for c in allowed_companies.sorted("id"))
        )
        print(f"company_id active -> {active_company.name}({active_company.id})")
        print(
            "credential_delivery="
            + (
                "auth_signup self-service link (ONLY if res.partner.signup_url "
                "exists -- absent on Odoo 19, then falls back to temp password)"
                if signup_installed
                else "random temp password (auth_signup missing)"
            )
        )
        print("No write, no commit.")
        print("----END CEO_PROVISION_DRYRUN----")
        return 0

    # company_ids: (4, cid) links are additive -- never drops an already-allowed
    # company. company_id (active) is set in the SAME write and is guaranteed by
    # _resolve_companies to be within the allowed set (constraint safe).
    company_link_cmds = [(4, c.id) for c in allowed_companies]
    if user:
        # Promote a possibly-portal/share account: drop the exclusive Portal/
        # Public groups (if held) in the SAME write that adds the internal/admin
        # set, else _check_disjoint_groups aborts. Unlinks first, then (4, gid)
        # adds -- additive on groups the user already has.
        group_cmds = _exclusive_group_removals(user, groups_field) + [
            (4, gid) for gid in group_ids
        ]
        user.write({
            "name": CEO_NAME,
            "email": CEO_EMAIL,
            groups_field: group_cmds,
            "company_ids": company_link_cmds,
            "company_id": active_company.id,
        })
        _err(f"updated existing user uid={user.id}, ensured CEO/admin groups + companies")
    else:
        user = env["res.users"].create({
            "name": CEO_NAME,
            "login": CEO_LOGIN,
            "email": CEO_EMAIL,
            groups_field: [(6, 0, group_ids)],
            "company_ids": company_link_cmds,
            "company_id": active_company.id,
        })
        _err(f"created user uid={user.id}")

    env.cr.commit()
    print(f"CEO_ODOO_UID={user.id}")
    print(
        "CEO_ODOO_COMPANY_IDS="
        + ",".join(str(c.id) for c in user.company_ids.sorted("id"))
    )
    print(f"CEO_ODOO_ACTIVE_COMPANY_ID={user.company_id.id}")

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

    # Odoo 19 removed res.partner.signup_url (verified live on prod 2026-08-31:
    # `AttributeError: 'res.partner' object has no attribute 'signup_url'`), so
    # the self-service-link path is dead there even with auth_signup installed.
    # Only take it when the attribute actually exists; otherwise fall through to
    # the direct temp-password delivery below -- the path that unblocked the CEO.
    if signup_installed and hasattr(user.partner_id, "signup_url"):
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
