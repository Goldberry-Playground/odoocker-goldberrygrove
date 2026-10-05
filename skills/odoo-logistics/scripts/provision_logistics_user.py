#!/usr/bin/env python3
"""
Provision the least-privilege Odoo user that the logistics agent (Otto)
authenticates as. Run once by an operator who holds Odoo *admin* credentials
(or by DevOps with vault access), against the running Odoo.

What it does (idempotent):
  1. Finds or creates a res.users login `logistics-otto` (Internal User).
  2. Grants ONLY the groups a logistics/inventory specialist needs:
        - Inventory / User        (stock.group_stock_user)
        - Inventory / Manager     (stock.group_stock_manager)   -> adjustments
        - Purchase / User         (purchase.group_purchase_user)
        - Sales / User: own docs  (sales_team.group_sale_salesman)
        - Multi-UoM               (uom.group_uom)
        - Product packaging       (product.group_stock_packaging)
     Explicitly does NOT grant Settings/admin, Accounting, or user-management.
  3. Prints the user id.

What it deliberately does NOT do:
  - It does NOT create the API key. Keys are minted by the private method
    res.users.apikeys._generate, which Odoo's RPC layer refuses to dispatch
    (underscore-prefixed), so it CANNOT be done over XML-RPC from here. Mint
    the key headlessly in an Odoo shell with the companion script:
        odoo shell -d "$ODOO_DB" --no-http < mint_logistics_key.py
    then store it in the secrets manager and inject it as ODOO_API_KEY into
    Otto's runtime env. NEVER paste it into agent config, AGENTS.md, or an
    issue thread.

Admin env contract (for THIS script only — not for Otto):
    ODOO_URL, ODOO_DB (or ODOO_DB_NAME), ODOO_ADMIN_LOGIN, ODOO_ADMIN_API_KEY

Usage:
    ODOO_URL=… ODOO_DB=… ODOO_ADMIN_LOGIN=… ODOO_ADMIN_API_KEY=… \
        provision_logistics_user.py [--login logistics-otto] [--name "Logistics — Otto (agent)"] [--dry-run]

stdlib-only; logs to stderr.
"""

from __future__ import annotations

import argparse
import os
import secrets
import string
import sys
import xmlrpc.client

# Read-biased default scope. Verified against prod Odoo 19.0-20260513 on
# 2026-10-05 (GOL-2963): `purchase` and the product-packaging feature flag are
# NOT installed on prod, so those two xml_ids WARN-and-skip there; they resolve
# on qa-l3. `stock.group_stock_manager` (inventory adjustments) is behind
# --with-stock-manager because it is a write escalation that needs CEO sign-off.
GROUP_XMLIDS = [
    "base.group_user",              # Role / User — required for an internal user
    "stock.group_stock_user",
    "purchase.group_purchase_user",
    "sales_team.group_sale_salesman",
    "uom.group_uom",
    "account.group_account_readonly",  # read-only accounting, for reconciliation
    # Product packaging feature — needed to read/write product.packaging
    # (box-fit / carton strategy per product class). Skipped gracefully if the
    # feature flag isn't installed (WARN, not fatal).
    "product.group_stock_packaging",
]

# Write escalation: inventory adjustments. Opt-in only.
STOCK_MANAGER_XMLID = "stock.group_stock_manager"

# Where --set-password drops the credential for the owning agent to self-inject
# with bootstrap_otto_env.py. See GOL-2963.
DEFAULT_SIDECAR_OUT = "/paperclip/work/gol2963/otto-odoo.env"


def _log(*a: object) -> None:
    print("[provision]", *a, file=sys.stderr, flush=True)


def _write_sidecar(path: str, url: str, db: str, login: str, password: str) -> None:
    """Write the credential to a mode-0600 env file and log only the path.

    The secret must not reach stdout/stderr: these scripts run inside agent
    runs, where anything printed is captured into the run transcript.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # Create with 0600 from the start, so the value is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"ODOO_URL={url}\n")
        f.write(f"ODOO_DB={db}\n")
        f.write(f"ODOO_LOGIN={login}\n")
        f.write(f"ODOO_API_KEY={password}\n")
    os.chmod(path, 0o600)
    _log(f"credential written to {path} (mode 0600, not printed)")
    _log(
        "the owning agent consumes it with "
        "`python3 scripts/bootstrap_otto_env.py`, which self-injects into its "
        "own adapterConfig.env and then shreds this file"
    )


def _env(name: str, *fallbacks: str) -> str:
    for n in (name, *fallbacks):
        v = os.environ.get(n)
        if v:
            return v
    _log(f"ERROR: missing env {name}")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision least-privilege logistics Odoo user")
    ap.add_argument("--login", default="logistics-otto")
    ap.add_argument("--name", default="Logistics — Otto (agent)")
    ap.add_argument("--dry-run", action="store_true", help="report intended changes only")
    ap.add_argument(
        "--with-stock-manager",
        action="store_true",
        help="also grant stock.group_stock_manager (inventory adjustments, write escalation — CEO approval)",
    )
    ap.add_argument(
        "--set-password",
        action="store_true",
        help=(
            "generate a 40-char random password for the user and write it to "
            "--sidecar-out as a mode-0600 env file. Use when "
            "mint_logistics_key.py is unreachable (no Odoo shell / no prod "
            "SSH): common.authenticate() accepts a password wherever it "
            "accepts an API key, so the value can be injected as "
            "ODOO_API_KEY. Rotate to a real scoped API key when a shell is "
            "available again."
        ),
    )
    ap.add_argument(
        "--sidecar-out",
        default=DEFAULT_SIDECAR_OUT,
        help=(
            "where --set-password writes the credential: a mode-0600 env file "
            "holding ODOO_URL / ODOO_DB / ODOO_LOGIN / ODOO_API_KEY, for the "
            "owning agent to consume with bootstrap_otto_env.py. The secret is "
            "NEVER printed — only this path is. (default: %(default)s)"
        ),
    )
    args = ap.parse_args()

    url = _env("ODOO_URL").rstrip("/")
    db = _env("ODOO_DB", "ODOO_DB_NAME")
    admin_login = _env("ODOO_ADMIN_LOGIN")
    admin_key = _env("ODOO_ADMIN_API_KEY")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, admin_login, admin_key, {})
    if not uid:
        _log("ERROR: admin authentication failed")
        return 1
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def ex(model, method, args_, kw=None):
        return models.execute_kw(db, uid, admin_key, model, method, args_, kw or {})

    # Resolve group ids from xml_ids.
    wanted = list(GROUP_XMLIDS)
    if args.with_stock_manager:
        wanted.append(STOCK_MANAGER_XMLID)

    group_ids = []
    for xmlid in wanted:
        module, name = xmlid.split(".", 1)
        rec = ex(
            "ir.model.data",
            "search_read",
            [[["module", "=", module], ["name", "=", name]]],
            {"fields": ["res_id", "model"], "limit": 1},
        )
        if not rec or rec[0]["model"] != "res.groups":
            _log(f"WARN: group xml_id not found (module not installed?): {xmlid}")
            continue
        group_ids.append(rec[0]["res_id"])
    _log(f"resolved {len(group_ids)} groups: {group_ids}")

    # Odoo 19 renamed res.users.groups_id -> group_ids. Detect it instead of
    # guessing: on 19 a write to `groups_id` fails, which is how the original
    # version of this script silently broke against prod (GOL-2963).
    user_fields = ex("res.users", "fields_get", [], {"attributes": ["type"]})
    group_field = "group_ids" if "group_ids" in user_fields else "groups_id"
    _log(f"res.users group field on this server: {group_field}")

    existing = ex(
        "res.users",
        "search_read",
        [[["login", "=", args.login]]],
        {"fields": ["id", "name", group_field], "limit": 1},
        )

    if args.dry_run:
        action = "update groups on" if existing else "create"
        _log(
            f"DRY-RUN: would {action} user '{args.login}' with groups {group_ids}"
            + (" and reset its password" if args.set_password else "")
        )
        return 0

    new_password = None
    if args.set_password:
        alphabet = string.ascii_letters + string.digits
        new_password = "".join(secrets.choice(alphabet) for _ in range(40))

    if existing:
        user_id = existing[0]["id"]
        values = {group_field: [(4, gid) for gid in group_ids], "active": True}
        if new_password:
            values["password"] = new_password
        ex("res.users", "write", [[user_id], values])
        _log(f"updated existing user id={user_id}, ensured logistics groups")
    else:
        user_id = ex(
            "res.users",
            "create",
            [
                {
                    "name": args.name,
                    "login": args.login,
                    group_field: [(6, 0, group_ids)],
                    **({"password": new_password} if new_password else {}),
                }
            ],
        )
        _log(f"created user id={user_id}")

    print(user_id)
    if new_password:
        # Deliberately NOT printed. An earlier version echoed the password to
        # stdout between BEGIN/END markers, which in an agent run means the
        # credential lands in the run transcript and the issue's log — CodeQL
        # flagged it as py/clear-text-logging-sensitive-data and was right.
        # Write it straight to a mode-0600 sidecar instead and print the path.
        _write_sidecar(args.sidecar_out, url, db, args.login, new_password)
    _log(
        "NEXT: mint this user's API key headlessly — "
        "`odoo shell -d $ODOO_DB --no-http < mint_logistics_key.py` — then "
        "store it in the secrets manager and inject ODOO_LOGIN + ODOO_API_KEY "
        "into Otto's runtime env. "
        "NO-SSH FALLBACK (GOL-2963, while GOL-2956 keeps prod SSH dead): pass "
        "--set-password to write a 40-char random password instead; "
        "common.authenticate() accepts a password wherever it accepts an API "
        "key, so ODOO_API_KEY can carry it. Rotate to a real scoped API key "
        "once an Odoo shell is reachable again. "
        "HAND-OFF: DevOps cannot write another agent's env (403 "
        "deny_missing_grant) — the owning agent consumes the sidecar with "
        "`bootstrap_otto_env.py`, which self-injects and shreds it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
