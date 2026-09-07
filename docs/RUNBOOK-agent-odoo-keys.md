# Runbook — Per-agent Odoo users, API keys & MCP auth hardening

**Owner:** DevOps-Terra · **Tickets:** GOL-2095 (WS3c), under GOL-2092 (WS3 epic)
**Status:** scripts + workflow codified (this PR). LIVE provisioning is gated on
the SSH + 1Password write-back secrets (same set as GOL-89) and CEO approval —
see [Go-live gates](#go-live-gates). Nothing here mutates prod until then.

## What this is / why

The Asana → Odoo Projects cutover (GOL-2092) makes Odoo Projects the single
source of truth, with **agents writing `project.task` through the Odoo MCP**.
Today that MCP authenticates with **one shared CEO/admin API key** (`odoo-mcp-qa`,
uid=2, full-admin, scope=NULL — the "YOLO key"). That is the wrong blast radius:
every agent shares one credential that can do *anything* in Odoo (Accounting,
Settings, user management), and a leak or a buggy agent is unbounded and
unattributable.

This runbook replaces that with **one least-privilege Odoo user per agent**, each
holding **its own `rpc`-scoped API key**, so:

- **Least privilege** — an agent key can read/write `project.task` and nothing
  else. No Accounting, no Settings, no user management, no storefront-bearer use
  (rpc scope ≠ the NULL scope storefront keys need).
- **Attribution** — every task write is stamped with that agent's user, in the
  Odoo chatter/log, not "Administrator".
- **Blast radius / revocation** — compromise or misbehaviour is contained to one
  agent and revoked by deleting one key, without touching the others or the
  storefront.

## Identities (roster)

Source of truth: `scripts/provision_project_agents_shell.py` → `ROSTER`.

| Login | Kind | Tier | Groups granted |
|---|---|---|---|
| `agent-ada` | agent | user | `base.group_user`, `project.group_project_user` |
| `agent-terra` | agent | user | same |
| `agent-iris` | agent | user | same |
| `agent-penny` | agent | user | same |
| `agent-sora` | agent | user | same |
| `agent-otto` | agent | user | same |
| Wes / George / Abigail | human | user | same — **once their email is filled into `ROSTER`** |

Notes:
- `agent-otto` is **separate** from the pre-existing `logistics-otto`
  (GOL-89, Inventory/Purchase/Sales scope). This runbook does not touch that
  user; Projects is a different capability and gets its own least-privilege
  identity.
- **Humans are free** in Odoo Community (internal users) but need a real email
  to log in. Roster entries with no email are **SKIPPED with a WARN** — safe to
  provision agents now and add humans later by editing `ROSTER` and re-running.
- **`manager` tier** (adds `project.group_project_manager`: create projects,
  manage stages) is available per-entry but granted to **nobody** by default.
  The WS3b migration bot is the intended consumer; grant it deliberately, not
  broadly.

## Provisioning (create users + mint keys)

Fully automated via **`.github/workflows/provision-project-agents.yml`**
(`workflow_dispatch`, dry-run default). It SSHes to the droplet and runs the two
shell scripts inside `docker compose exec odoo odoo shell` (superuser `env`,
no admin login needed — see the scripts' headers for why XML-RPC can't do this).

1. **Dry-run first** — dispatch with `environment=qa`, `dry_run=true`. It prints
   the resolved roster + groups + companies and **writes nothing**. Confirm the
   scope preview shows `project.group_project_user` and NOT accounting/settings.
2. **Live** — dispatch with `dry_run=false`. It provisions the whole roster
   (idempotent), then mints each agent's `rpc`-scoped key and writes it to
   `op://Grove CI Writeback/<login>-<env>/ODOO_API_KEY` (+ `ODOO_LOGIN`). The
   write-back vault + token are the same ones the Otto workflow uses — see
   [`RUNBOOK-otto-key-writeback.md`](./RUNBOOK-otto-key-writeback.md).

Manual equivalent (operator with droplet SSH), e.g. one agent:

```bash
set -a; . /etc/grove/.env; set +a          # or /opt/grove/.env on prod
cd /etc/grove
# 1. create/ensure the user (whole roster; idempotent, additive)
docker compose exec -T odoo \
    odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
    < scripts/provision_project_agents_shell.py
# 2. mint that agent's key (repeat per login)
docker compose exec -T -e AGENT_LOGIN=agent-ada odoo \
    odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
    < scripts/mint_agent_key_shell.py
# -> plaintext key printed ONCE between ----BEGIN/END AGENT_API_KEY---- markers.
#    Store it, inject it (below), then clear scrollback.
```

## MCP auth hardening — wiring keys into agents

Each agent's Odoo MCP profile must authenticate as **its own** user. The key is
injected the same durable way every other agent secret is (control-plane
`adapterConfig.env`, read into process env on every run — see the
`op-token-runtime-injection` memory), as a **1Password reference, never
plaintext**:

Per agent, set in `adapterConfig.env` (an `agents:create` caller — CEO runtime):

| Env var | Value |
|---|---|
| `ODOO_URL` | `https://odoo.qa.gatheringatthegrove.com` (qa) / `https://odoo.gatheringatthegrove.com` (prod) |
| `ODOO_DB` | `odoo` |
| `ODOO_LOGIN` | that agent's login, e.g. `agent-ada` |
| `ODOO_API_KEY` | `op://Grove CI Writeback/agent-ada-<env>/ODOO_API_KEY` |

Then **retire the shared key from the agent path**: no agent's env should carry
`odoo-mcp-qa`/uid=2 anymore. Keep that admin key ONLY as a break-glass /
provisioning credential in `Grove Prod` (and `Grove QA`), wired to no agent MCP.

Verify each agent: with its injected creds, a `project.task` read/write
succeeds and an Accounting read is **denied** (proves the scope is tight, not
just that auth works). Attribution check: the task's log shows the agent user,
not Administrator.

## Key rotation

The mint script is **idempotent by key name**: it revokes any prior key named
`<login>-mcp-runtime` for that user before minting a fresh one, so re-running
always leaves exactly one working key of known provenance.

**Routine rotation** (per agent, or all):
1. Re-run `provision-project-agents.yml` with `dry_run=false` (or the manual
   mint step for one login). The old key is revoked and a new one written to the
   same 1P item — the `op://…` reference in the agent's env is unchanged.
2. The next agent run reads the new key automatically (env is resolved per run).
   No config edit needed because the env holds a **reference**, not the value.
3. Confirm the old key is gone: it no longer appears in
   `res.users.apikeys` for that user (see [Audit](#audit--verification)).

Rotate the whole roster by dispatching the workflow with no per-agent filter
(it loops every login in `AGENT_LOGINS`).

## Key revocation (incident / offboarding)

**Fast path (revoke one agent, keep the rest):** delete that user's key(s) in an
Odoo shell — deletion is via SQL because ORM `unlink` / the public `remove()`
path are gated by an interactive identity re-check we can't satisfy headlessly
(same reason the mint script deletes by SQL):

```bash
cd /etc/grove   # or /opt/grove on prod
docker compose exec -T odoo odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null <<'PY'
login = "agent-ada"                      # the compromised/offboarded identity
u = env["res.users"].search([("login", "=", login)], limit=1)
keys = env["res.users.apikeys"].sudo().search([("user_id", "=", u.id)])
if keys:
    env.cr.execute("DELETE FROM res_users_apikeys WHERE id IN %s", (tuple(keys.ids),))
    env["res.users.apikeys"].sudo().invalidate_model()
    env.cr.commit()
    print(f"revoked {len(keys)} key(s) for {login} (uid={u.id})")
else:
    print(f"no keys for {login}")
PY
```

The agent's MCP calls start failing auth immediately. To restore service, re-mint
(above). To **fully offboard** an agent, also deactivate the user
(`u.active = False`) and clear its `op://…` item.

**Full lockdown (revoke everything):** delete all rows in `res_users_apikeys`
for every roster user; this also kills the storefront bearer keys if you widen
the domain, so scope the `user_id IN (...)` filter to the roster uids only unless
a total lockdown is intended.

**After any revocation:** rotate the 1Password write-back items so a stale value
can't be re-injected, and note the incident on the owning ticket.

## Audit / verification

List a user's live keys (read works over XML-RPC with the admin key, or in a
shell) — the key value is never shown, only its metadata:

```python
u = env["res.users"].search([("login", "=", "agent-ada")], limit=1)
for k in env["res.users.apikeys"].sudo().search([("user_id", "=", u.id)]):
    print(k.id, k.name, k.scope, k.create_date)
```

Confirm least privilege: the user should hold `base.group_user` +
`project.group_project_user` (+ `project.group_project_manager` only if manager
tier) and **no** `base.group_system` / `account.*`.

## Go-live gates

- **Josh** — seed the droplet SSH secrets (`QA_SSH_PRIVATE_KEY`/`QA_HOST`,
  `PROD_SSH_PRIVATE_KEY`/`PROD_HOST`) and the `OP_CI_WRITEBACK_SA_TOKEN` write
  token (`Grove CI Writeback` vault). Same set GOL-89 has been waiting on;
  reconcile the existing `GROVE_QA_CI_SSH_PRIVATE_KEY` name at go-live.
- **CEO** — approve the LIVE run (creates users + mints keys on QA, then prod)
  and the retirement of the shared `odoo-mcp-qa` key from the agent path. This
  is a money-flow-adjacent auth change to production Odoo, so it is a board gate,
  not self-servable.
- **Humans** — supply Wes/George/Abigail emails before their entries are
  un-skipped.

## Residual risks (accepted, documented)

- **Minted keys transit the runner argv** (`op item edit` takes values as args),
  briefly visible in the runner's process table. Same accepted risk as the Otto
  writeback; the runner is ephemeral and single-tenant.
- **qa/prod share one write-back vault + token** — item naming (`<login>-<env>`)
  keeps them distinct; worst case is availability (an agent env holding a
  wrong-env key), not disclosure. Split into two vaults if true env isolation is
  wanted later.
- **Company scope = all entities.** Roster users are allowed every `res.company`
  (avoids the GOL-1811 invisible-entity trap for cross-entity project work). If
  an agent should be entity-scoped, set `AGENT_COMPANIES` when provisioning.
