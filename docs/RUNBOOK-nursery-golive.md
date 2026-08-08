# Runbook — Nursery storefront go-live (GOL-484, revenue #1)

**Status:** READY, gated on Josh (interaction `5ca1d29a`: launch-catalog choice +
a ~30-min coordinated window). Board GO-LIVE approved 2026-08-07 (interaction
`5b2c6abb`: grove-sites `nursery` + Stripe path, **live Stripe spend = YES**,
DevOps-Terra authorized on prod creds). Owner: **DevOps - Terra**. Acceptance
(one live test order) returns to the assignee once the site takes an order.

This is the single, ordered checklist for the coordinated window. It **stitches
together existing, tested pieces** — it does not invent a new deploy path. Every
step below is either an existing script/runbook or a named human action.

> ⚠️ **Money flow.** This enables real card charges for At the Grove Nursery,
> LLC. Do not run steps 3–7 outside the coordinated window with Josh present.
> No secret VALUES appear in this doc or the repo.

---

## Preconditions (all TRUE as of 2026-08-07 — re-verify at top of window)

| # | Precondition | Evidence / source |
|---|--------------|-------------------|
| P1 | Prod grove_headless module pin = `8accb94` on **both** codified paths | `docker-compose.override.production.yml` `GITSYNC_REF`; `infra/terraform/environments/production/variables.tf` `custom_modules_ref` default. Merged in **odoocker #419**. |
| P2 | Prod Odoo is **bare** (grove_headless `uninstalled`, only default company id 1) → go-live is a **fresh `-i`**, not a migration; no data-loss / pre-migrate / reconciliation risk | GOL-1258 prod-DB check; GOL-1214 sign-off. |
| P3 | Target SHA `8accb94` exposes `/grove/api/v1/products \| cart \| shipping \| stripe/webhook` (live prod 404s on these today) | GOL-484 recon; SHA composition documented in `variables.tf` `custom_modules_ref`. |
| P4 | Live Stripe keys present in `Grove Prod/stripe` vault (`atthegrovenursery_stripe_key` = `rk_live_` restricted; live acct `acct_16cKwAEkDEtb2GgD`, `charges_enabled: true`) | GOL-484 comment 2026-08-07, Josh-confirmed. |
| P5 | `grove-nursery-prod` App Platform app ACTIVE & healthy | App ID `b9e0d2a6-6495-4dc7-a069-015b653c87e9`, ingress `grove-nursery-prod-peoxi.ondigitalocean.app`. |

Re-verify P1 quickly:

```sh
grep GITSYNC_REF docker-compose.override.production.yml   # → 8accb943d664a78df69f79eab689e7024d2b2445
grep -A1 'variable "custom_modules_ref"' -n infra/terraform/environments/production/variables.tf
```

---

## Josh's launch-catalog decision (interaction `5ca1d29a`, question `catalog`)

A fresh `-i grove_headless` seeds the scaffold but **no products** → prod `/shop`
is empty. The runbook branches on Josh's answer:

- **(a) `seed_fixture`** — seed the QA nursery fixture catalog into prod now
  (fastest to live sales). Do **step 4a** after the rebuild.
- **(b) `hold_curated`** — hold for Wesley's curated real inventory. Skip step
  4a; go-live can still complete the rebuild + wiring, but the acceptance order
  waits for a real product to exist.
- **(c) `discuss`** — pause; do not run steps 4a–7.

---

## Go-live sequence (coordinated window)

### 1. Rebuild prod Odoo at the pinned SHA (fresh install)

The durable path is automatic: `odoo/entrypoint.sh` runs a blocking
`--init=base,<mods> --update=<mods>` at boot whenever git-sync advances the
modules revision (`AUTO_UPGRADE_MODULES`, set in the prod compose). Because
grove_headless is uninstalled, this resolves to `--init=base,grove_headless`
(a **fresh install**, not an upgrade).

Two ways to trigger it — pick one:

- **Droplet replace (immutable, preferred):** `terraform apply` in
  `infra/terraform/environments/production/`. The new cloud-init carries
  `custom_modules_ref=8accb94`; git-sync checks it out and the entrypoint runs
  the fresh init on first boot. Apply from the operator machine whose /32 is in
  `var.admin_ip_cidr` (managed-PG grants + firewall already permit it — see
  `docs/RUNBOOK-managed-pg-odoo-bootstrap.md`; both fresh-cluster gotchas are
  already codified and the cluster persists across replaces, so a replaced
  droplet boots clean).
- **In-place on the running droplet (escape hatch):** if the droplet already
  runs at `8accb94` but the module never installed, force the entrypoint to
  re-run on the current revision:

  ```sh
  QA_HOST=root@<prod-odoo-host> DEPLOY_DIR=/etc/grove scripts/module-upgrade.sh
  ```

  (Clears `/mnt/odoo-filestore/.grove-modules-rev` + restarts odoo; reuses the
  same tested `--init=base,grove_headless` code path — never an ad-hoc
  `docker run odoo -u ...`.)

### 2. Verify the module installed and routes are live

```sh
curl -s https://odoo.gatheringatthegrove.com/web/health          # → {"status":"pass"} (200)
curl -s -o /dev/null -w '%{http_code}\n' https://odoo.gatheringatthegrove.com/grove/api/v1/products   # → 200 (was 404)
```

Confirm in the DB that grove_headless is `installed` and the tenant companies
landed (company id 1 → "Goldberry Grove Farm"; nursery tenant at id 2/3,
slug-resolved → zero app-code impact per GOL-1214).

### 3. Inject the live nursery Stripe key

grove_headless reads its Stripe secret from the **lowercase** process env
`stripe_test_secret_key` — the single point of failure for ALL checkout (see
`scripts/check-checkout-secrets-wired.sh`). Inject the live **nursery backend**
key `atthegrovenursery_stripe_key` (`rk_live_`) as `stripe_test_secret_key` in
the prod Odoo env (ggg/goldberry keys parked for now). Do **not** paste the
value into any file tracked by git.

### 4. Register the live Stripe webhook  → **JOSH action**

Register the live webhook endpoint (→ `/grove/api/v1/stripe/webhook`) in the
At the Grove Nursery Stripe account. Stripe issues a `whsec_…` signing secret.
**Josh stores the `whsec_` in the `Grove Prod` vault** (Terra's op SA is
read-only there — cannot write it). Then verify:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://odoo.gatheringatthegrove.com/grove/api/v1/stripe/webhook   # → 200/400 (reachable), not 404
```

### 4a. Seed the launch catalog  *(only if Josh chose `seed_fixture`)*

Load the nursery fixture catalog into prod via the proven XML-RPC seed path
(GOL-641/655/668), retargeted from `odoo.qa/xmlrpc/2` to the prod Odoo XML-RPC
endpoint, using the prod Odoo API key from the `Grove Prod` vault. Publish the
canonical tree/cultivar products so `/shop` renders cards. Confirm:

```sh
curl -s https://odoo.gatheringatthegrove.com/grove/api/v1/products | head   # → non-empty product list
```

### 5. Server-side checkout smoke on the App Platform URL (pre-DNS)

Before any DNS change, smoke a **live** checkout session against the nursery
app's App Platform ingress (decoupled from the 4-brand apex cutover):

```sh
# expect a cs_live_ session id back (mirrors the QA cs_test_ smoke, GOL-701/702)
curl -s https://grove-nursery-prod-peoxi.ondigitalocean.app/api/checkout/session -X POST ...
```

Verify the session id on `api.stripe.com` shows the live account.

### 6. Point the nursery domain

The `atthegrovenursery.com` apex flip is part of the **coordinated 4-brand
one-way-door cutover** (`docs/RUNBOOK-apex-launch-cutover.md`, GOL-287,
CEO-gated). Do **not** flip the nursery apex in isolation as part of this
runbook unless that cutover is already done. Fastest revenue path: take the
acceptance order on the App Platform URL (step 5/7); schedule the apex flip via
GOL-287 with the CEO. Re-read the DO ingress suffix at the top of that window —
it changes if the app is recreated.

### 7. Acceptance — one live test order  → **JOSH action**

Agents cannot enter card data. **Josh places one small real order** end-to-end
(browse → product → cart → checkout → pay with a live card), confirms the order
lands in prod Odoo routed to the nursery sales team with correct tax/shipping,
then **refunds** it. Report with evidence (order id + Stripe `pi_`/`ch_` id +
tax/shipping lines). This is the GOL-484 acceptance criterion.

### 8. Post-go-live smoke

```sh
bash scripts/smoke-test-public.sh   # includes https://atthegrovenursery.com
```

---

## Rollback

- **Module install misbehaves:** the fresh `-i` runs on a bare managed PG
  (only default company id 1), so blast radius is minimal. Re-drive with
  `scripts/module-upgrade.sh` after a fix; or `terraform apply` a droplet
  replace at the previous pin (revert #419) — but the previous pin has **no
  checkout**, so this is a revert-to-pre-launch, not a partial rollback.
- **Checkout inert (503 "not configured"):** `stripe_test_secret_key` unset or
  webhook secret missing — re-run steps 3–4; `scripts/check-checkout-secrets-wired.sh`
  guards the ref presence.
- **DNS (if step 6 ran):** apex rollback is owned by the GOL-287 cutover
  runbook (revert the Origin Rule / A record); do not improvise here.

## References

- odoocker #419 (SHA pin) · `variables.tf` `custom_modules_ref` (SHA rationale)
- `docs/RUNBOOK-managed-pg-odoo-bootstrap.md` · `docs/RUNBOOK-module-upgrade.md`
- `docs/RUNBOOK-checkout-stripe-guardrails.md` · `docs/RUNBOOK-apex-launch-cutover.md`
- `scripts/module-upgrade.sh` · `scripts/check-checkout-secrets-wired.sh` · `scripts/smoke-test-public.sh`
- GOL-1214 (modules sign-off) · GOL-1258 (prod-DB check) · GOL-641/655/668 (seed path)
