# RUNBOOK: Checkout / Stripe / secrets — guardrails & best practices

Owner: DevOps (Terra) · Created 2026-07-30 (GOL-899) · Applies to: QA and prod checkout

This is the standing reference for anyone — engineering or devops — touching the
**checkout flow, Stripe keys, or the secrets that feed them**. It exists because
we broke checkout twice in a week through changes that each looked safe in
isolation. Read the incident first; the rules after it are the concrete lessons.

---

## 1. The incident (audit — what actually happened)

**Symptom (GOL-956, ~2026-07-28):** every storefront's checkout returned a fast
`504`. Buyers could not pay on nursery, ggg, or goldberry QA.

**Root cause chain:**

1. There is exactly **one** Stripe key the live checkout path reads: the Odoo
   backend key `stripe_test_secret_key` (lowercase env var, read by
   `grove_headless` `controllers/main.py` via `os.environ.get`). The checkout
   **session** route is `/grove/api/v1/checkout/session`.
2. GOL-890 shipped thin-proxy frontends (grove-sites PR #278 ggg, #279 goldberry;
   nursery already): the Next.js storefronts stopped calling Stripe directly and
   now **all three proxy to that one Odoo route**. That single env var became the
   single point of failure for *all* checkout.
3. During the per-tenant Stripe key rework (GOL-688/696), a prior devops change
   **commented out** `TF_VAR_stripe_test_secret_key` in
   `infra/terraform/environments/qa-app-platform/.env.op`. The intent was good —
   mint a *dedicated* backend key instead of reusing a storefront key (scope
   purity; avoid coupling). But the replacement was never provisioned.
4. The Terraform variable `stripe_test_secret_key` **defaults to `""`**, so
   `terraform plan` and `apply` stayed green. `grove_headless` degraded
   *gracefully* to `503 {"error":"Checkout is not configured yet"}`. No error,
   no failed deploy, no alert.
5. DigitalOcean's App Platform edge rewrites the app's `5xx` into an opaque
   `504` at the browser (see GOL-956), so even the symptom pointed away from
   "missing secret."

**Net:** a clean plan + a graceful degrade + an opaque edge error = **checkout
dead for ~a week with zero signal.**

**The fix (GOL-899, Josh's call 2026-07-30):** point the backend key at the
existing `stripe-nursery-qa` Grove QA item (its restricted `rk_test_` scope is
empirically sufficient — it creates live Checkout Sessions, GOL-956). For **QA**
the backend and nursery storefront share one key; that coupling is accepted in
QA and **must not** be carried to prod (see §4).

---

## 2. The failure modes to internalize

- **A "graceful degrade" on a revenue path is an outage, not a no-op.** An empty
  Stripe key that yields a friendly 503 is worse than a crash, because nothing
  goes red. Treat "checkout returns not-configured" as sev-worthy.
- **An empty Terraform variable default hides missing secrets.** `default = ""`
  keeps `plan` working without op access (good) but means the *only* place the
  gap surfaces is at apply time, resolved silently to `""`. The default is a
  convenience for planning, never a statement that empty is acceptable.
- **`op://` refs resolve against whatever op identity runs the apply.** If that
  account can't read the vault, the ref resolves to `""` — same silent failure.
  The QA apply account **must** be `grove-devops-ro` (reads `Grove QA` + Admin);
  the Admin-only SA resolves every `Grove QA` ref to empty.
- **One shared route = one shared blast radius.** After GOL-890 there is no
  per-tenant checkout backend. A single misconfigured env breaks all storefronts
  at once. Changes to that route/env are inherently high-blast-radius.

---

## 3. Rules — before you touch checkout, Stripe, or its secrets

1. **Never disable a live secret without its replacement already wired.** Do not
   comment out / null a secret ref "to provision the real one later." If you must
   stage, land the replacement ref in the same change so the resolved value is
   never empty. If you cannot, the change is blocked, not merged.
2. **Trace the whole path before editing.** For checkout that is:
   storefront (grove-sites) → `/grove/api/v1/checkout/session` (grove_headless) →
   `os.environ["stripe_test_secret_key"]` ← `/etc/grove/.env` ← cloud-init
   `user_data` ← `var.stripe_test_secret_key` ← `.env.op` `op://` ref ← 1Password.
   A break anywhere in that chain 503s checkout.
3. **Verify at apply time, not plan time.** A green `plan` proves nothing about
   secret presence. After any checkout-affecting apply, run the smoke test in §5.
4. **Least privilege, but wire the identity you actually need.** The apply op
   account must be able to read the vault the refs point at. Confirm it's
   `grove-devops-ro` before applying a checkout change.
5. **Secret VALUES never enter the repo, a diff, a comment, or a log.** Only
   `op://` references (UUID + field-id per GOL-394) live in `.env.op`. If you
   see a raw `sk_test_`/`rk_test_`/`whsec_` in a diff, stop and escalate.
6. **Changing `stripe_test_*` forces a droplet REPLACE.** These flow through
   cloud-init `user_data`; changing them re-provisions the QA Odoo droplet.
   Safe (reserved IP re-points DNS; filestore is a durable DO block volume) but
   it is a replace, not an in-place update — treat it as a real deploy and
   confirm blast radius before applying. Prod: board-gated (ADR-007).
7. **The CI guard is a floor, not a ceiling.** `scripts/check-checkout-secrets-wired.sh`
   (CI job `checkout Stripe secrets wired`) reds if a required Stripe ref is
   commented/missing. It only checks ref *presence*, not that the value resolves
   or the key has the right scope. It cannot replace the §5 smoke test.

---

## 4. Prod carry-forward (do NOT repeat the QA shortcut)

QA shares the `stripe-nursery-qa` key between the nursery storefront and the Odoo
backend. That is a pragmatic QA-only choice. **Production must not.** Prod needs a
**dedicated backend key** so that revoking a storefront key never disables the
checkout backend (and vice versa). Track this before any prod checkout enablement:
mint a dedicated backend key (`sk_test_`/live equivalent, or a restricted key
scoped to session + webhook operations), store it as its own 1Password item, and
wire it to `stripe_test_secret_key` in the prod env's `.env.op`.

**Prod arming is codified in `infra/terraform/environments/production/.env.op`**
(GOL-973 block). Two hard gates before the key is wired:

1. **Own item, own vault.** The live key is minted + vaulted in **`Grove Prod`**
   (same vault as the Shippo LIVE key), NOT the `Goldberry Grove - Admin` infra
   vault, and NEVER a storefront item.
2. **Scope-verify before wiring.** Stripe's dashboard restricted-key presets are
   over-broad (they hand out customer/charge/balance reads). Prove the minted key
   is checkout-only *before* it touches `.env.op`:
   ```
   op run -- python3 qa-tools/check_stripe_key_scopes.py \
     --op-ref 'op://Grove Prod/stripe-checkout-backend-prod/secret_key'
   ```
   The tool is behavioral (Stripe exposes no grant list): it probes live
   endpoints read-only and requires Checkout-Sessions **write** present + every
   forbidden capability **403-denied**. Only `VERDICT: PASS` clears the key to be
   wired; `FAIL` (over-broad or missing scope) => re-mint a narrower key. The key
   is never passed on argv and never printed — only a masked fingerprint.

---

## 5. Post-change smoke test (run after any checkout apply)

Static (safe anywhere, no secrets, no network to Stripe):
```
bash scripts/check-checkout-secrets-wired.sh
```

Live (requires a per-tenant Odoo bearer key; probes the deployed droplet):
```
# 503 "Checkout is not configured yet" => backend key is EMPTY (regression).
# 400 "success_url required"            => key IS set (healthy — expected here).
# 401                                   => bad bearer, unrelated to Stripe.
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://odoo.qa.gatheringatthegrove.com/grove/api/v1/checkout/session \
  -H "Authorization: Bearer <per-tenant odoo api key>" \
  -H 'Content-Type: application/json' -d '{}'
```
Run it for **all three** tenants (goldberry, ggg, nursery) — they share the
backend, so all three must return the same healthy status. Per-tenant bearer keys
live in Admin `Grove Infra` → `odoo_api_keys_tf_json`.

---

## 6. Related

- `.env.op` and `variables.tf` in `infra/terraform/environments/qa-app-platform/`
  and `.../production/` carry inline pointers back to this runbook.
- `qa-tools/check_stripe_key_scopes.py` — the scope-verify gate tool (§4). Used
  for both QA and prod keys; run before wiring any restricted key.
- ADR-007 — droplet-replace model (reserved IP + durable filestore volume).
- GOL-890 — thin-proxy checkout architecture (why one env is the SPOF).
- GOL-956 — DO edge rewrites app 5xx to an opaque browser 504.
- GOL-942 — client is resilient to a non-JSON error body (no parser leak).
