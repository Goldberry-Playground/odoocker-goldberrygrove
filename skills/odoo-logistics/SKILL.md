---
name: odoo-logistics
description: >-
  Read/write the self-hosted Grove Odoo (products, inventory moves, vendor
  pricelists, purchasing, and read-only sales/finance for Stripe/Shippo
  reconciliation) via a least-privilege XML-RPC client. Use for any logistics
  or inventory task that needs live Odoo data. Credentials come from the runtime
  environment — never from agent config.
---

# Odoo Logistics Access

This skill is the Logistics & Inventory Specialist's safe entry point into the
self-hosted Grove Odoo (the system of record for products, inventory, orders,
purchasing, and accounting). It wraps Odoo's XML-RPC external API with a
least-privilege policy so you can work directly in Odoo without admin rights
and without any secrets in your config.

## When to use

- Look up product / variant / category / UoM master data.
- Check on-hand quantities, inventory moves, pickings, lots, warehouses.
- Set reorder points / safety stock (`stock.warehouse.orderpoint`) and manage
  packaging / box-fit (`product.packaging`).
- Read or update vendor pricelists and supplier info.
- Read / create / update purchase-order **drafts** (confirming a PO is CFO-only
  — the `state` field is refused here; draft and hand off).
- Read sales orders + deliveries for fulfilment and for reconciling
  Stripe / Shippo against Odoo (sales & finance are **read-only** here).

## Entry point

Everything runs through one CLI (stdlib Python, no dependencies):

```
scripts/odoo_client.py <command> …
```

Start every session with a connectivity + policy check:

```
scripts/odoo_client.py check
```

That prints the authenticated uid, the Odoo server version, and the exact
read/write allow-lists in force.

### Reading

```
# What fields does a product have?
scripts/odoo_client.py fields product.template --attrs string,type

# All storable products, first 50, just a couple of fields
scripts/odoo_client.py search-read product.template \
  --domain '[["type","=","product"]]' \
  --fields default_code,name,qty_available,list_price --limit 50

# On-hand for one product across locations
scripts/odoo_client.py search-read stock.quant \
  --domain '[["product_id","=",42]]' \
  --fields location_id,quantity,reserved_quantity

# Open purchase orders
scripts/odoo_client.py search-read purchase.order \
  --domain '[["state","in",["draft","sent","purchase"]]]' \
  --fields name,partner_id,amount_total,date_order --order 'date_order desc'
```

`search`, `count`, and `read --ids 1,2,3` are also available. Domains and field
values are JSON.

### Writing (gated)

Mutating calls only work on inventory / purchasing / master-data models and
**require `--confirm`**. Every write is logged to stderr before it runs.

```
# Update a vendor price (create a pricelist item)
scripts/odoo_client.py create product.supplierinfo \
  --values '{"partner_id":7,"product_tmpl_id":42,"price":3.25,"min_qty":10}' \
  --confirm

# Correct a product's internal reference
scripts/odoo_client.py write product.template \
  --ids 42 --values '{"default_code":"PKG-BOX-M"}' --confirm
```

Deletes (`unlink`) are blocked entirely. `account.*`, `payment.transaction`,
`sale.order`, and `res.partner` are **read-only** — money, CRM, and revenue
docs cannot be mutated through this skill. Confirming a purchase order
(`purchase.order.state` → `purchase`) is also refused: it commits binding spend
and is CFO-only, so draft the PO here and hand off the confirmation. If you have
a legitimate need for a model or a write that policy blocks, ask Engineering to
widen the allow-list on [GOL-57](/GOL/issues/GOL-57) rather than working around
it.

See `references/models.md` for the full scoped model list and the Odoo security
groups they map to, and `references/reconciliation.md` for the Stripe/Shippo
reconciliation surface.

## Security model (why this is safe)

1. **No secrets in config.** The client reads `ODOO_URL`, `ODOO_DB`,
   `ODOO_LOGIN`, `ODOO_API_KEY` from the runtime environment only. Nothing is
   hard-coded; nothing is written to disk.
2. **Least-privilege Odoo user.** You authenticate as `logistics-otto`, an
   Internal User with only Inventory + Purchase + Sales groups — not admin.
   (Provisioned by `scripts/provision_logistics_user.py`.)
3. **Defense-in-depth allow-lists.** Even with those groups, this client
   refuses any model/method outside the logistics scope, gates every write
   behind `--confirm`, and blocks deletes.

## Environment contract

Injected by the operator via the secrets manager into your runtime — **never**
in AGENTS.md, adapterConfig, or an issue thread:

| Var            | Prod value                                | Purpose                    |
| -------------- | ----------------------------------------- | -------------------------- |
| `ODOO_URL`     | `https://odoo.gatheringatthegrove.com`    | Odoo base URL              |
| `ODOO_DB`      | `odoo`                                    | database name              |
| `ODOO_LOGIN`   | `logistics-otto`                          | scoped API user (prod uid 13) |
| `ODOO_API_KEY` | *(secret)*                                | that user's Odoo key/password |

If `check` reports missing env, the credentials have not been injected yet —
see **First-run bootstrap** immediately below.

## First-run bootstrap (GOL-2963) — run this once if `check` says env is missing

Per-agent env lives in `agents.adapter_config.env`, and `PATCH /api/agents/:id`
only accepts the **owning agent itself** (`allow_self`) or a holder of
`agents:create`. DevOps has neither for someone else's agent — it gets
`403 deny_missing_grant` — so DevOps **cannot** inject this for you. The split is:

- **DevOps mints** the credential (needs prod Odoo admin, which this user does
  not have) and leaves it in a mode-0600 sidecar file on the box.
- **You self-inject** it with one command, which then shreds the sidecar:

```
python3 scripts/bootstrap_otto_env.py            # add --dry-run first if you want
```

It reads `/paperclip/work/gol2963/otto-odoo.env`, PATCHes your own
`adapterConfig.env` with the four vars, re-reads the agent to confirm the keys
landed, runs `odoo_client.py check` plus a real `search-read` inline as proof,
then shreds the sidecar. Idempotent. The secret never passes through an issue
thread, a comment, or AGENTS.md.

⚠️ Adapter env is read at **run start**, so the PATCH is visible to your *next*
run, not the one that did the PATCH. That is why the script runs the check
inline — that output is the proof, no second heartbeat needed.

If the sidecar is gone (already consumed, or shredded), ask DevOps-Terra to
re-mint it: `provision_logistics_user.py --set-password` is idempotent.

## Provisioning the credential (operator, one-time)

Done once by DevOps against the running Odoo; the agent never does this.

1. **Create the least-privilege user** (over XML-RPC, needs admin creds):
   ```
   ODOO_URL=… ODOO_DB=… ODOO_ADMIN_LOGIN=… ODOO_ADMIN_API_KEY=… \
     scripts/provision_logistics_user.py --dry-run   # confirm scope, then drop --dry-run
   ```
2. **Mint the user's API key** — headless, in an Odoo shell (NOT XML-RPC: the
   `_generate` method is underscore-prefixed and the RPC layer refuses it):
   ```
   docker compose exec -T odoo \
     odoo shell -d "$ODOO_DB" --no-http < scripts/mint_logistics_key.py
   ```
   The key prints once between `----BEGIN/END LOGISTICS_OTTO_API_KEY----`.
   Both scripts are idempotent — safe to re-run for rotation/recovery.
   **No-SSH fallback (current prod reality — GOL-2956 killed prod SSH):** the
   Odoo shell is unreachable, so use `provision_logistics_user.py
   --set-password` instead. `common.authenticate()` accepts a password wherever
   it accepts an API key, so the 40-char random password can carry
   `ODOO_API_KEY`. It is weaker (it also permits web-UI login) — rotate to a
   real scoped key once a shell is reachable again (GOL-2993).
3. **Hand off, don't inject:** DevOps cannot write another agent's env (403
   `deny_missing_grant`). `--set-password` already writes the four vars to a
   mode-0600 sidecar (`--sidecar-out`, default
   `/paperclip/work/gol2963/otto-odoo.env`) and **never prints the secret** —
   printing it would put the credential in the agent run transcript. Then have
   the owning agent run `scripts/bootstrap_otto_env.py` (see **First-run
   bootstrap** above), which self-injects and shreds the sidecar.

## Prod Odoo shape traps (verified 2026-10-05 on 19.0-20260513)

- **`purchase` is NOT installed on prod.** `purchase.group_purchase_user` does
  not resolve and the entire purchase-order surface documented above does not
  exist on prod. It does exist on qa-l3.
- **`product.group_stock_packaging` is not installed** → no `product.packaging`
  / box-fit work on prod.
- **Odoo 19 renamed `res.users.groups_id` → `group_ids`.** Detect with
  `fields_get`; never guess.
- `stock.group_stock_manager` (inventory adjustments) is **deliberately
  withheld** — it is a write escalation needing CEO sign-off. `unlink` is
  refused server-side for `product.template`, verified.
