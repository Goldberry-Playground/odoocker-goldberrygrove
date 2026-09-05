# -*- coding: utf-8 -*-
"""
Provision the least-privilege Odoo Projects roster HEADLESSLY, inside an Odoo
shell (full DB context, superuser `env`). GOL-2095 (WS3c, under GOL-2092): the
Asana -> Odoo Projects cutover moves agents onto PER-AGENT Odoo users + API
keys, replacing the single shared CEO/admin "YOLO" MCP key. This script creates
(or ensures) one internal user per agent + the named human collaborators, each
scoped to `project.task` write and nothing else -- no Accounting, no Settings,
no user management.

Companion to mint_agent_key_shell.py: run THIS first (create users + grant
scoped groups), then mint_agent_key_shell.py per agent to generate that user's
`rpc`-scoped API key. Both run the same GOL-89 mechanism the CEO ratified for
`logistics-otto` (docker compose exec odoo odoo shell), generalised to a roster.

    # provision the whole roster (idempotent, additive):
    docker compose exec -T odoo \
        odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/provision_project_agents_shell.py

    # or one login only (matches the per-agent CI matrix):
    docker compose exec -T -e AGENT_LOGIN=agent-ada odoo \
        odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/provision_project_agents_shell.py

WHY A SHELL SCRIPT (not XML-RPC)
    On the self-hosted droplets the only admin credential present is the Odoo
    **master password** (odoo.conf `admin_passwd`, /etc/grove/.env
    ODOO_ADMIN_PASSWORD) -- that gates /web/database management, it is NOT a
    res.users login, so there is nothing for XML-RPC to authenticate as. Inside
    `odoo shell`, `env` already runs as SUPERUSER, so no credential is needed.
    Same mechanism as provision_ceo_user_shell.py and provision_logistics_*.

LEAST PRIVILEGE (project.task write, no accounting)
    REQUIRED for every roster member:
        base.group_user               Internal User (mandatory backend base)
        project.group_project_user    Project / User -> read+write on
                                      project.task within accessible projects
    "manager" tier ADDS (only for members who must create projects / manage
    stages, e.g. the migration bot):
        project.group_project_manager Project / Administrator
    Deliberately NOT granted to anyone: base.group_system (Settings),
    base.group_erp_manager (Access Rights), account.* (Accounting),
    any Sales/Inventory/Purchase admin. A member that is a hand-made
    portal/share account is promoted to internal in the SAME write that drops
    the mutually-exclusive base.group_portal / base.group_public, or Odoo's
    _check_disjoint_groups aborts (same trap the CEO script documents).

COMPANY SCOPING (GOL-1811 / GOL-1842)
    The Grove DB is multi-company (nursery / GGG / farm as separate res.company).
    A user with no explicit company_ids is left on whatever default the DB picks,
    so the other entities' projects are invisible. Roster members are given
    company_ids = ALL companies (additive) with the lowest-id company active, so
    project.task in any entity is reachable. Override with AGENT_COMPANIES
    (';'-separated names) / AGENT_MAIN_COMPANY if a member should be entity-scoped.

HUMANS
    Wes, George and Abigail are included but require a real email (Community
    internal users are free but must have a login). A human roster entry with no
    email is a WARN and is SKIPPED -- safe to run now for agents, later for
    humans once their emails are supplied via the ROSTER edit below.

IDEMPOTENT / DRY RUN
    Find-or-create by login; groups + companies ensured with (4, id) links
    (additive, never clobbers). Safe to re-run. Set AGENT_DRY_RUN=1 to print the
    resolved roster + groups + companies and WRITE NOTHING (no create, no commit).
    Prints nothing sensitive -- keys are NOT minted here (see mint_agent_key_shell.py).
"""

import os
import sys

DRY_RUN = os.environ.get("AGENT_DRY_RUN", "").strip() not in ("", "0", "false", "False")
# Optional single-login filter (CI matrix runs one agent at a time). Empty =
# the whole roster.
ONLY_LOGIN = os.environ.get("AGENT_LOGIN", "").strip()
# Optional company override (see header). Semicolon-separated because a company
# name may contain a comma ("At The Grove Nursery, LLC").
AGENT_COMPANIES = os.environ.get("AGENT_COMPANIES", "").strip()
AGENT_MAIN_COMPANY = os.environ.get("AGENT_MAIN_COMPANY", "").strip()

# ── The roster (single source of truth) ───────────────────────────────────────
# kind:  "agent" (a Paperclip agent's MCP identity) | "human" (a person)
# tier:  "user"    -> base.group_user + project.group_project_user
#        "manager" -> the above + project.group_project_manager (create projects,
#                     manage stages -- e.g. the WS3b migration bot). Keep this
#                     list SHORT; least privilege is the default.
# email: required for humans; None means "not yet supplied" -> skipped with a WARN.
#        Agents log in by login only (no mailbox), email left None intentionally.
#
# Agent logins use the `agent-<name>` convention so they are visually distinct
# from human logins and from the pre-existing `logistics-otto` (Otto's separate
# inventory-scoped identity from GOL-89, which this does NOT touch).
ROSTER = [
    # Paperclip agents -> per-agent MCP identity on project.task.
    {"login": "agent-ada",   "name": "Engineering - Ada (agent)",  "kind": "agent", "tier": "user",    "email": None},
    {"login": "agent-terra", "name": "DevOps - Terra (agent)",     "kind": "agent", "tier": "user",    "email": None},
    {"login": "agent-iris",  "name": "Frontend - Iris (agent)",    "kind": "agent", "tier": "user",    "email": None},
    {"login": "agent-penny", "name": "Penny (agent)",              "kind": "agent", "tier": "user",    "email": None},
    {"login": "agent-sora",  "name": "Sora (agent)",               "kind": "agent", "tier": "user",    "email": None},
    {"login": "agent-otto",  "name": "Logistics - Otto (agent, projects)", "kind": "agent", "tier": "user", "email": None},
    # Human collaborators -> supply real emails before provisioning (free in
    # Community). Left None = SKIPPED until an email is filled in here.
    {"login": None, "name": "Wes",     "kind": "human", "tier": "user", "email": None},
    {"login": None, "name": "George",  "kind": "human", "tier": "user", "email": None},
    {"login": None, "name": "Abigail", "kind": "human", "tier": "user", "email": None},
]

REQUIRED_GROUP_XMLIDS = [
    "base.group_user",            # Internal User (mandatory backend base)
    "project.group_project_user", # Project / User -> read+write project.task
]
MANAGER_GROUP_XMLIDS = [
    "project.group_project_manager",  # Project / Administrator (manager tier only)
]


def _err(*a):
    print("[provision-agents]", *a, file=sys.stderr, flush=True)


# `env` is injected by `odoo shell`. Fail loudly if run the wrong way.
try:
    env  # noqa: F821  (provided by the shell namespace)
except NameError:
    _err(
        "ERROR: `env` is not defined. Run this INSIDE an Odoo shell, e.g.\n"
        "  docker compose exec -T odoo odoo shell -d \"$DB_NAME\" --no-http "
        "< scripts/provision_project_agents_shell.py"
    )
    raise SystemExit(2)


def _groups_field():
    """Odoo 19 renamed res.users.groups_id -> group_ids. Resolve at runtime so
    this survives the 17/18 (groups_id) <-> 19+ (group_ids) straddle -- writing
    the wrong name raises ValueError before any commit. Prod is 19.0."""
    fields = env["res.users"]._fields
    return "group_ids" if "group_ids" in fields else "groups_id"


def _resolve_group_ids(tier):
    """Resolve the group xml_ids for a tier -> [ids]. A missing REQUIRED group
    (wrong DB / project app not installed) raises; a missing manager group is a
    WARN (manager tier degrades to user rather than aborting the whole run)."""
    xmlids = list(REQUIRED_GROUP_XMLIDS)
    if tier == "manager":
        xmlids += MANAGER_GROUP_XMLIDS
    ids, missing_required = [], []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if not rec or rec._name != "res.groups":
            if xmlid in REQUIRED_GROUP_XMLIDS:
                missing_required.append(xmlid)
                _err(f"ERROR: REQUIRED group missing: {xmlid}")
            else:
                _err(f"WARN: manager group not found (project app?): {xmlid}")
            continue
        ids.append(rec.id)
    return ids, missing_required


def _exclusive_group_removals(user, groups_field):
    """(3, gid) unlink cmds for any Portal/Public group the found user holds --
    they are mutually exclusive with base.group_user and must be dropped in the
    SAME write that adds the internal group, else _check_disjoint_groups aborts.
    Returns [] for an already-internal user (the common re-run case)."""
    removals = []
    current_ids = getattr(user, groups_field).ids
    for xmlid in ("base.group_portal", "base.group_public"):
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec and rec._name == "res.groups" and rec.id in current_ids:
            removals.append((3, rec.id))
            _err(f"will drop exclusive group {xmlid} -> res.groups({rec.id})")
    return removals


def _resolve_companies():
    """(allowed_recordset, active_record). allowed = AGENT_COMPANIES names or
    ALL companies; active = AGENT_MAIN_COMPANY or the lowest-id allowed company,
    always unioned into allowed so company_id-in-company_ids holds."""
    Company = env["res.company"].sudo()
    all_companies = Company.search([], order="id")
    if AGENT_COMPANIES:
        allowed = Company.browse()
        missing = []
        for name in [n.strip() for n in AGENT_COMPANIES.split(";") if n.strip()]:
            rec = Company.search([("name", "=", name)], limit=1)
            allowed |= rec if rec else Company.browse()
            if not rec:
                missing.append(name)
        if missing:
            _err(f"WARN: AGENT_COMPANIES names not found (skipped): {missing}")
        if not allowed:
            _err("WARN: no AGENT_COMPANIES matched; using ALL companies")
            allowed = all_companies
    else:
        allowed = all_companies
    active = Company.browse()
    if AGENT_MAIN_COMPANY:
        active = Company.search([("name", "=", AGENT_MAIN_COMPANY)], limit=1)
        if not active:
            _err(f"WARN: AGENT_MAIN_COMPANY '{AGENT_MAIN_COMPANY}' not found; falling back")
    if not active:
        active = allowed.sorted("id")[0]
    allowed |= active
    return allowed, active


def _effective_roster():
    """Roster after applying the AGENT_LOGIN filter and dropping humans with no
    email (WARN). Agents keep their login; humans without a login/email are
    skipped so the script is safe to run before human emails are supplied."""
    out = []
    for m in ROSTER:
        login = m["login"]
        if m["kind"] == "human" and not (login or m.get("email")):
            _err(f"WARN: human '{m['name']}' has no login/email yet -- SKIPPED "
                 "(fill it into ROSTER before provisioning humans)")
            continue
        # A human entry may carry only an email; use it as the login.
        if not login:
            login = m.get("email")
        if not login:
            continue
        if ONLY_LOGIN and login != ONLY_LOGIN:
            continue
        out.append({**m, "login": login})
    return out


def _upsert(member, groups_field, allowed_companies, active_company):
    login = member["login"]
    group_ids, missing_required = _resolve_group_ids(member["tier"])
    if missing_required:
        _err(f"ABORT: required groups missing {missing_required} -- project app installed?")
        return None
    company_link_cmds = [(4, c.id) for c in allowed_companies]
    user = env["res.users"].search([("login", "=", login)], limit=1)
    if user:
        group_cmds = _exclusive_group_removals(user, groups_field) + [(4, gid) for gid in group_ids]
        vals = {groups_field: group_cmds, "company_ids": company_link_cmds,
                "company_id": active_company.id}
        if member.get("email"):
            vals["email"] = member["email"]
        user.write(vals)
        _err(f"updated uid={user.id} login='{login}' tier={member['tier']}")
    else:
        vals = {"name": member["name"], "login": login,
                groups_field: [(6, 0, group_ids)],
                "company_ids": company_link_cmds, "company_id": active_company.id}
        if member.get("email"):
            vals["email"] = member["email"]
        user = env["res.users"].create(vals)
        _err(f"created uid={user.id} login='{login}' tier={member['tier']}")
    return user


def main():
    groups_field = _groups_field()
    _err(f"res.users groups m2m field -> {groups_field}")
    roster = _effective_roster()
    if not roster:
        _err("no roster members to process (filter matched nothing / humans unset)")
        return 0
    allowed_companies, active_company = _resolve_companies()
    _err("companies: allowed=" + ", ".join(f"{c.name}({c.id})" for c in allowed_companies.sorted("id"))
         + f" | active={active_company.name}({active_company.id})")

    if DRY_RUN:
        print("----BEGIN AGENT_PROVISION_DRYRUN----")
        print(f"groups_m2m_field={groups_field}")
        print("companies_allowed=" + ";".join(c.name for c in allowed_companies.sorted("id")))
        print(f"company_active={active_company.name}")
        for m in roster:
            existing = env["res.users"].search([("login", "=", m["login"])], limit=1)
            gids, missing = _resolve_group_ids(m["tier"])
            action = f"UPDATE(uid={existing.id})" if existing else "CREATE"
            grps = "base.group_user+project.group_project_user" + (
                "+project.group_project_manager" if m["tier"] == "manager" else "")
            miss = f" MISSING_REQUIRED={missing}" if missing else ""
            print(f"{m['kind']:5} login={m['login']:14} tier={m['tier']:7} "
                  f"action={action} groups={grps}{miss}")
        print("granted: base.group_user + project.group_project_user "
              "(+ project.group_project_manager for manager tier)")
        print("NOT granted: Settings/admin, Access Rights, Accounting, "
              "Sales/Inventory/Purchase admin, user management")
        print("No write, no commit. Keys are NOT minted here (see mint_agent_key_shell.py).")
        print("----END AGENT_PROVISION_DRYRUN----")
        return 0

    provisioned = []
    for m in roster:
        user = _upsert(m, groups_field, allowed_companies, active_company)
        if user is not None:
            provisioned.append((m["login"], user.id))
    env.cr.commit()
    print("----BEGIN AGENT_PROVISION_RESULT----")
    for login, uid in provisioned:
        print(f"{login}={uid}")
    print("----END AGENT_PROVISION_RESULT----")
    _err(f"provisioned {len(provisioned)} user(s). NEXT: mint each agent's key with "
         "mint_agent_key_shell.py (AGENT_LOGIN=<login>, also in an odoo shell).")
    return 0


raise SystemExit(main())
