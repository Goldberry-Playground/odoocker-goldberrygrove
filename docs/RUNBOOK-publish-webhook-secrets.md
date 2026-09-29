# Runbook — QA publish-webhook HMAC secrets

Tracking issue: **GOL-2518** (child of GOL-2337; feature tickets GOL-985/986/1004,
sellout fast path GOL-1896).

## What the secret does

Each QA tenant has ONE shared HMAC secret with two halves that must be
**byte-identical**:

| Half | Where it lives | Set by |
| --- | --- | --- |
| Sender (Odoo) | droplet `/etc/grove/.env` → `GROVE_PUBLISH_WEBHOOK_SECRET_<TENANT>` | `cloud-init.yaml.tpl` (rewritten on droplet rebuild) |
| Receiver (storefront) | DO App `grove-<tenant>-qa` env `GROVE_PUBLISH_WEBHOOK_SECRET` | `apps.tf` |

Both read `var.grove_publish_webhook_secret_<tenant>`, so Terraform keeps them
matched — as long as that variable actually has a value.

## The failure mode this runbook exists for

`variables.tf` defaults all three to `""`. If the `.env.op` ref is commented
out, **every apply and every release-train `make qa-l3-up` overwrites the live
secret with `""` on both halves.**

It fails **silently**. The Odoo sender raises before any `grove.publish.event`
row is written (nothing in the audit log), and the receiver returns 401 on every
delivery. The storefront just degrades to the 30s ISR backstop, so `/shop` looks
*almost* right. goldberry was live-provisioned 2026-07-30 and was found empty on
both halves on 2026-09-23 — dead for ~8 weeks, unnoticed.

## The guard

`scripts/check-publish-webhook-secrets-wired.sh` — run automatically:

- `make qa-l3-up` — **hard gate**: aborts before `terraform init`/`apply`.
- `make qa-l3-plan` — warn-only (`PUBLISH_SECRET_GUARD_WARN_ONLY=1`); plan is
  read-only so it still runs.
- `make qa-check-publish-secrets` — run it on its own.

Two modes, picked automatically. Inside `op run` it requires each
`TF_VAR_grove_publish_webhook_secret_<tenant>` to be **set and non-empty** (this
catches a ref that resolves to a blank vault field). Outside `op run` it falls
back to a static check that the `op://` ref in `.env.op` is uncommented. It
never reads, prints, or compares secret VALUES.

Deliberate override — you accept zeroing the secrets:

```bash
ALLOW_EMPTY_PUBLISH_SECRETS=1 make qa-l3-up
```

## Making it durable (the one-time fix)

Needs 1Password **write** on vault `Grove QA`. The ops service account is
read-only there (`op item create` → `(101) You do not have permission`), so a
human does this.

### ⏳ Step 1 expires at the next train teardown

`make train-teardown` destroys `digitalocean_app.tenant` (the three tenant
storefront apps) **and** `digitalocean_droplet.odoo` — which are the only two
places an un-vaulted publish-webhook secret exists. First teardown: **Thu
2026-09-24**.

So there are two versions of this fix, and which one you get is decided by the
teardown, not by you:

- **Before teardown** — nursery's secret is live and working. Copy it
  (step 1 below) so the tenant keeps publishing without a re-key.
- **After teardown** — nothing to preserve. **Skip step 1**; mint all three
  with `openssl rand -hex 32`. This is strictly simpler, just with nursery's
  current working value lost. `doctl apps spec get` for a destroyed app returns
  an error, not a secret — do not read that as "the secret was empty".

The guard does **not** protect against this: it refuses an apply that would zero
a live secret, but a teardown is a deliberate destroy and is not gated.

1. Read the currently-live nursery secret — it is already correct on both ends
   and must be **preserved, not re-minted**:
   ```bash
   doctl apps spec get aa671f09-2a3a-42a3-a648-760095b289bc --format json \
     | jq -r '.services[].envs[] | select(.key=="GROVE_PUBLISH_WEBHOOK_SECRET") | .value'
   ```
2. In vault `Grove QA`, create one item per tenant, each with a field named
   `secret`:
   - `grove-publish-webhook-nursery-qa` → the value from step 1 (byte-identical).
   - `grove-publish-webhook-goldberry-qa` → fresh `openssl rand -hex 32`
     (both live halves are already empty — nothing to preserve).
   - `grove-publish-webhook-ggg-qa` → fresh `openssl rand -hex 32`
     (never provisioned).
3. Uncomment the three `TF_VAR_grove_publish_webhook_secret_*` refs in
   `infra/terraform/environments/qa-app-platform/.env.op`. **This is a human
   step too**: `.env.op` is under `infra/terraform/**`, a protected path, so it
   needs human review + merge. The change is pre-written as odoocker **PR #731**
   (draft on purpose — an `op://` ref to an item that does not exist yet is a
   hard `op run` failure, which would wedge `qa-l3-plan`/`qa-l3-up` for
   everyone). Mark it ready and merge it once the items from step 2 exist.
4. `make qa-check-publish-secrets` → PASS, then `make qa-l3-plan`, then
   `make qa-l3-up`.

After that, 1Password is the source of truth and a droplet rebuild or a
train teardown reconciles instead of zeroing.

## Verifying end to end

Per tenant, against the promoted build:

```bash
python3 scripts/qa_verify/availability_invalidation.py   # in grove-odoo-modules
```

Sell the last unit of an in-stock species; within ~5s `/shop` must show it sold
out with no manual Publish, and exactly one `grove.publish.event` row
(`event_type product.availability`) must exist for it.

## Related

- `scripts/check-checkout-secrets-wired.sh` / `docs/RUNBOOK-checkout-stripe-guardrails.md`
  — same "empty default is not a safe no-op" lesson, Stripe edition (GOL-899).
- Odoo-side sender wiring for all three tenants: odoocker PR #727.
