# -*- coding: utf-8 -*-
"""
Mint the three QA storefront bearer keys HEADLESSLY, inside `odoo shell`.

WHY (2026-09-08): the QA `qa-l3-up` apply pushed `TF_VAR_odoo_api_keys` into
the three grove-*-qa App Platform specs, and QA's `.env.op` resolves that var
to the SAME 1Password field production uses (`Grove Infra/odoo_api_keys_tf_json`).
That field now holds the 2026-09-06 prod keys of the "Storefront Service"
user (uid 11) -- a user QA's database does not have -- so every authenticated
storefront call on QA 401s (surfaced as an edge 504). QA needs its OWN keys,
minted on QA, stored in a QA-only 1Password field.

WHAT IT DOES
    For each tenant (goldberry, ggg, nursery): revoke any prior key with the
    same name on the owner user (clean rotation, exactly one working key per
    name), mint a NEW key with scope NULL -- the scope grove_headless bearer
    auth requires -- then print ONE JSON map between markers:

        ----BEGIN ODOO_API_KEYS_TF_JSON----
        {"goldberry": "...", "ggg": "...", "nursery": "..."}
        ----END ODOO_API_KEYS_TF_JSON----

    That JSON is the exact value `TF_VAR_odoo_api_keys` (a `map(string)`)
    expects. Odoo stores only a hash: the plaintext is unrecoverable after this
    print. Capture it straight into 1Password; never paste it anywhere else.

WHY A SHELL SCRIPT (not XML-RPC)
    res.users.apikeys._generate is a private method; the RPC dispatcher refuses
    `_`-prefixed calls, so keys can only be minted in-process (same reason as
    odoocker scripts/mint_agent_key_shell.py, whose idiom this mirrors).

RUN (on the QA odoo droplet, from /etc/grove)
    docker compose exec -T odoo \
        odoo shell -d odoo --no-http --logfile=/dev/null \
        < mint_storefront_keys_qa_shell.py

ENV (optional)
    OWNER_LOGIN   res.users login to own the keys. Default "admin" -- the same
                  identity production's LIVE storefront keys sit on (uid 2,
                  keys "grove-storefront-<tenant>" from 2026-08-31).
    KEY_PREFIX    key-name prefix. Default "grove-storefront" (prod naming).
"""
import inspect
import json
import os
import sys

TENANTS = ("goldberry", "ggg", "nursery")
OWNER_LOGIN = os.environ.get("OWNER_LOGIN", "admin").strip() or "admin"
KEY_PREFIX = os.environ.get("KEY_PREFIX", "grove-storefront").strip() or "grove-storefront"
SCOPE = None  # NULL scope: what grove_headless `auth="bearer"` endpoints check


def _err(*a):
    print("[mint-storefront-qa]", *a, file=sys.stderr, flush=True)


try:
    env  # noqa: F821  (injected by `odoo shell`)
except NameError:
    _err("ERROR: run this INSIDE `odoo shell`: docker compose exec -T odoo "
         "odoo shell -d odoo --no-http < mint_storefront_keys_qa_shell.py")
    raise SystemExit(2)


def main():
    user = env["res.users"].sudo().search([("login", "=", OWNER_LOGIN)], limit=1)
    if not user:
        _err(f"ERROR: owner user '{OWNER_LOGIN}' not found; aborting (nothing minted).")
        return 2
    if not user.active:
        _err(f"ERROR: owner user '{OWNER_LOGIN}' is archived; bearer auth requires an "
             "ACTIVE user (res.users.apikeys._check_credentials filters on u.active). "
             "Reactivate first; aborting.")
        return 2

    Apikeys = env["res.users.apikeys"].sudo()
    generate = env["res.users.apikeys"].with_user(user)._generate
    kwargs = {}
    if "expiration_date" in inspect.signature(generate).parameters:
        kwargs["expiration_date"] = False  # non-expiring service credential

    minted = {}
    for tenant in TENANTS:
        key_name = f"{KEY_PREFIX}-{tenant}"
        prior = Apikeys.search([("user_id", "=", user.id), ("name", "=", key_name)])
        if prior:
            _err(f"revoking {len(prior)} prior key(s) named '{key_name}' on uid={user.id} (rotation)")
            env.cr.execute("DELETE FROM res_users_apikeys WHERE id IN %s", (tuple(prior.ids),))
            Apikeys.invalidate_model()
        minted[tenant] = generate(SCOPE, key_name, **kwargs)

    env.cr.commit()
    print("----BEGIN ODOO_API_KEYS_TF_JSON----")
    print(json.dumps(minted, separators=(",", ":")))
    print("----END ODOO_API_KEYS_TF_JSON----")
    _err(f"minted {len(minted)} NULL-scope keys on uid={user.id} login='{OWNER_LOGIN}' "
         f"({', '.join(f'{KEY_PREFIX}-{t}' for t in TENANTS)}). Store the JSON in 1Password "
         "(Grove QA), repoint qa-app-platform/.env.op, re-apply, clear scrollback.")
    return 0


raise SystemExit(main())
