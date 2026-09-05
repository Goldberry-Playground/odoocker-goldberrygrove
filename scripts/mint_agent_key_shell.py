# -*- coding: utf-8 -*-
"""
Mint a per-agent Odoo API key HEADLESSLY -- no self-service UI, no human login.
GOL-2095 (WS3c): companion to provision_project_agents_shell.py. Run the
provisioner first (creates the least-privilege project user), then run THIS,
once per agent, to generate that user's `rpc`-scoped API key for the Odoo MCP
layer. This is the generalised form of mint_logistics_key.py (GOL-89).

    docker compose exec -T -e AGENT_LOGIN=agent-ada odoo \
        odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/mint_agent_key_shell.py

WHY A SHELL SCRIPT (not XML-RPC)
    res.users.apikeys._generate is a PRIVATE method (leading underscore). Odoo's
    RPC dispatcher refuses to call any `_`-prefixed method, so a key CANNOT be
    minted over execute_kw. It must run in-process, inside an Odoo shell, where
    `env` is already bound as superuser.

REQUIRED ENV
    AGENT_LOGIN     the res.users login to mint for (e.g. agent-ada). REQUIRED --
                    there is no default, so a mis-invocation fails fast instead
                    of minting for the wrong identity.
    AGENT_KEY_NAME  optional key label; defaults to "<login>-mcp-runtime". The
                    name is the rotation handle: any prior key with this exact
                    name owned by this user is revoked before a fresh one is
                    minted, so re-running always leaves exactly ONE working key
                    of known provenance (clean rotation / recovery).

SCOPE
    `rpc` -- the scope Odoo checks when authenticating XML-RPC / JSON-RPC (the
    transport the Odoo MCP uses). NOT the NULL/full scope the storefront bearer
    keys use; an `rpc`-scoped key cannot be used as a storefront bearer, which is
    the point (least privilege, no scope crossover).

WHAT IT PRINTS
    The plaintext key ONCE, on stdout, between markers:
        ----BEGIN AGENT_API_KEY----
        <key>
        ----END AGENT_API_KEY----
    Odoo only stores a hash -- the key is unrecoverable after this. Capture it,
    write it to the secrets manager, inject it as ODOO_API_KEY into that agent's
    Paperclip runtime env, then clear scrollback. NEVER paste it into agent
    config, AGENTS.md, an issue thread, or any log artifact.

VERSION-ROBUST
    _generate gained an `expiration_date` parameter in Odoo 17; this introspects
    the live signature and only passes it when present (works 16/17/18/19).
"""

import inspect
import os
import sys

AGENT_LOGIN = os.environ.get("AGENT_LOGIN", "").strip()
AGENT_KEY_NAME = os.environ.get("AGENT_KEY_NAME", "").strip()
SCOPE = "rpc"  # scope Odoo checks when authenticating XML-RPC / JSON-RPC


def _err(*a):
    print("[mint-agent]", *a, file=sys.stderr, flush=True)


# `env` is injected by `odoo shell`. Fail loudly if run the wrong way.
try:
    env  # noqa: F821  (provided by the shell namespace)
except NameError:
    _err(
        "ERROR: `env` is not defined. Run this INSIDE an Odoo shell, e.g.\n"
        "  docker compose exec -T -e AGENT_LOGIN=agent-ada odoo "
        "odoo shell -d \"$DB_NAME\" --no-http < scripts/mint_agent_key_shell.py"
    )
    raise SystemExit(2)


def main():
    if not AGENT_LOGIN:
        _err("ERROR: AGENT_LOGIN is required (e.g. -e AGENT_LOGIN=agent-ada). Aborting.")
        return 2
    key_name = AGENT_KEY_NAME or f"{AGENT_LOGIN}-mcp-runtime"

    user = env["res.users"].search([("login", "=", AGENT_LOGIN)], limit=1)
    if not user:
        _err(f"ERROR: user '{AGENT_LOGIN}' not found. Run "
             "provision_project_agents_shell.py first.")
        return 2

    Apikeys = env["res.users.apikeys"].sudo()
    # Revoke prior keys with our marker name (idempotent re-run / rotation).
    # Delete via SQL -- ORM unlink and the public remove() path are gated by an
    # identity re-check we can't satisfy non-interactively in a shell.
    prior = Apikeys.search([("user_id", "=", user.id), ("name", "=", key_name)])
    if prior:
        _err(f"revoking {len(prior)} prior key(s) named '{key_name}' (rotation)")
        env.cr.execute("DELETE FROM res_users_apikeys WHERE id IN %s", (tuple(prior.ids),))
        Apikeys.invalidate_model()

    # Mint as the target user so the key is OWNED by that agent, not admin.
    generate = env["res.users.apikeys"].with_user(user)._generate
    kwargs = {}
    if "expiration_date" in inspect.signature(generate).parameters:
        kwargs["expiration_date"] = False  # non-expiring service credential
    key = generate(SCOPE, key_name, **kwargs)
    env.cr.commit()

    print("----BEGIN AGENT_API_KEY----")
    print(key)
    print("----END AGENT_API_KEY----")
    _err(f"minted OK for uid={user.id} login='{AGENT_LOGIN}' scope='{SCOPE}' "
         f"name='{key_name}'. Store in secrets manager, inject as ODOO_API_KEY "
         "into this agent's Paperclip env, clear scrollback.")
    return 0


raise SystemExit(main())
