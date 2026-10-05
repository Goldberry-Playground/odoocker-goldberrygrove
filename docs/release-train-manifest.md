# Grove Release Train — per-train manifest

Canon: vault `Software/Grove Release Train (QA Cadence)` (CEO-ratified 2026-09-20).
Tracking epic: **GOL-2324**. This file is the copy/paste template every train is
opened from, plus the **Train #1** worked example at the bottom.

## Why this exists

QA compute runs **only inside train windows** (biweekly), giving ~70% QA compute
reduction. Each train's bundle — which grove-sites PRs, which grove-odoo-modules
ref, which gate targets, which promote/reconcile plan — must be **explicit and
auditable per train**, not reconstructed from memory each fortnight. Fill one of
these out per train and keep it as the train's system of record.

## Cadence (fixed)

| Day | Action | Gate |
|-----|--------|------|
| Mon | `make train-up` (= `make qa-l3-up`) | Josh approval on spend (GOL-2326) |
| Mon–Wed | Bundle + gate on QA | `test:e2e:gate` + `@stripe`, cleanup first |
| Wed | Promote — **Leg B** `prod-modules-promote.sh` + `reconcile-modules-pin.yml`, then **Leg A** `promote-storefronts.yml`, then the named flag step | Josh production env-approval |
| Thu | `make train-teardown` (= `scripts/qa-l3-teardown.sh compute`) | keep PG/filestore/reserved-IP/DNS; NEVER `all` |

**Josh runs all three days** from his own shell — the `op://` refs live in the
`Goldberry Grove - Admin` vault, which no agent SA can read, and the prod
droplet's firewall does not admit the agent plane on port 22. **Terra conducts**:
bundle, this manifest, gate reading, promote PRs, verification. Full order of
operations for Wednesday: `RUNBOOK-release-train.md` → "The Wednesday promote
leg". The QA App Platform apps are **destroyed** each teardown (GOL-2327 Option
A) — not parked, not scaled to zero — and nothing is exempt: `grove-qa-l3-obs`
was retired 2026-09-29 (ADR-010).

---

## Manifest template (copy below this line)

```
### Train #N — week of YYYY-MM-DD

- **QA-up date (Mon):** YYYY-MM-DD  (`make qa-l3-up`)
- **Promote date (Wed):** YYYY-MM-DD
- **Teardown date (Thu):** YYYY-MM-DD

#### Bundle
- **grove-sites PRs:** #___ , #___  (one line each: what it changes + verify note)
- **grove-odoo-modules ref (backend):** `main HEAD` @ <SHA>  (list the money/compliance
  PRs this SHA carries so the release is explicit)

#### Gate (on QA)
- [ ] `scripts/qa-test-data-cleanup.sh` run BEFORE recording verdicts
- [ ] Catalog compat baseline captured BEFORE `qa-module-upgrade.sh`, verified
      after — `scripts/verify-shop-departments-compat.py` (see
      RUNBOOK-release-train.md). Required whenever the bundle carries a
      `product.public.category` migration.
- [ ] `test:e2e:gate` green
- [ ] `@stripe` suite green
- **Verdict:** PASS / FAIL @ <commit/run link>

#### Pre-promote (required, in this order — RUNBOOK-release-train.md "Order of operations")
- [ ] **QA `-u` soak at the pinned modules SHA:**
      `EXPECT_REF=<modules-sha> scripts/qa-module-upgrade.sh grove_headless`.
      `EXPECT_REF` makes it refuse a QA box that is not on the bundle SHA. Confirm
      the effects in the **database** (`ir_module_module.latest_version` + the
      rows/columns each migration writes) — a `-u` on a DB already past a
      migration's version legitimately *skips* it, so the log can be clean and the
      change absent. **No soak ⇒ the module half does not promote; it rides the
      next train.**
  - soak run / DB evidence: <fill>
- [ ] **e2e LAST:** re-dispatch `e2e-nursery.yml` on `main` *after* the soak and
      *after* the final merge, and **read the skip list** — the promo, WV-tax and
      Stripe-Tax specs must EXECUTE. Green with a skipped money spec is not a pass.
  - run link + skipped specs: <fill>

#### Promote (Wed — Josh approves production environment)
- [ ] Reconcile PRs deduped (no competing storefront pins) — winner: #___, closed: #___
- [ ] **Leg B (first, steps 3–5):** `TARGET_REF=<modules-sha> scripts/prod-modules-promote.sh`
      pre-flight, then `CONFIRM=PROMOTE`, then `reconcile-modules-pin.yml`. **NOT**
      "tag -> `release.yml`" and **NOT** an SSH `docker compose pull`. **Run it
      ONCE** — both Train #1 workarounds are fixed, so a failure now is a real
      failure. Modules before storefronts, so the frontend never goes live against
      an API missing its field.
- [ ] **Leg A (step 6):** `promote-storefronts.yml` with an **explicit**
      `target_sha`, run green with Josh env-approval. (Blank no longer means "main
      HEAD" — #821 resolves it to the newest `main` commit with a green
      `docker.yml` and verifies all four GHCR tags before the gate — but the bundle
      contract still wants the pinned SHA.)
- [ ] **Named flag step (step 7), only if this train carries one:** per-tenant env
      flags like `GROVE_STRIPE_TAX_{TENANT}` ship **OFF** and flip here **only if
      their QA gates passed**; otherwise they ride the next train (planned outcome,
      no escalation). **Rollback = flag off.**
  - gates passed? <fill: yes/no + evidence>  → flipped? <fill>
- **Storefront image pinned:** <SHA>  (`hub_image_tag` == `tenant_image_tag`)
- **Modules pin live on prod:** <SHA> (`CUSTOM_MODULES_REF` in `/etc/grove/.env`,
  the truth for Leg B) = grove_headless <version>
- **Prod verified:** rate probe / health-check link

#### Teardown (Thu — first-of-cycle compute-down)
- [ ] `bash scripts/qa-l3-teardown.sh compute`
- [ ] Survivors confirmed present (Managed PG, filestore + LE-cert volumes, reserved IP, DNS zone + CF delegation)
- [ ] Re-up verified at NEXT train (`make qa-l3-up` -> health green)
```

---

## Train #1 — week of 2026-09-21 (worked example — ACTUALS)

QA already up since 2026-09-08, so Train #1 had no distinct Mon train-up; the
biweekly Mon-up cadence starts at Train #2.

- **QA-up date:** 2026-09-08 (pre-cadence; already up)
- **Promote date (Wed):** 2026-09-23
- **Teardown date (Thu):** 2026-09-24 scheduled — **actually ran 2026-09-29**,
  five days late. That slip is why `release-train-reminder.yml` exists.

### Bundle
- **grove-sites PRs** (as actually shipped):
  - **#783** — FL mirror, merged 2026-09-21 (`zone_5`, per GOL-2345 / GOL-2235).
    ⚠️ Earlier revisions of this file listed **#737**; that PR was **closed
    unmerged** (draft, pre-addendum `zone_6`/`zone_7` shape) and never shipped.
  - #770 — estimator zone-map fix (merged 2026-09-21).
  - #750 — PDP copy (merged 2026-09-14).
  - #748 — gate (merged 2026-09-16).
- **grove-odoo-modules ref:** `main HEAD` — carries the **full GOL-2132 compliance
  stack (#214 / #215 / #217)**, so **FL ships in prod with this train, correctly
  substituted**. (Prod modules currently frozen @ `8accb94` per GOL-1791; this
  train's pin bump is the gated money release — confirm delta intended with CEO.)

### Gate
- [ ] cleanup, `test:e2e:gate`, `@stripe` — tracked in **GOL-2334**.

### Promote (Wed 2026-09-23)
- [ ] **Dedupe #666 vs #667 FIRST** — both open, both `mergeable_state: blocked`,
  competing storefront pins:
  - #666 -> `68f4e5c5` (GOL-2316, = live grove-sites #758 RCE bump)
  - #667 -> `578b828b`
  Pick the SHA matching the Train #1 bundle build, close the loser. Tracked in **GOL-2335**.
- [ ] odoocker pin bump + default-catch-up reconcile -> modules `main HEAD`.
- [ ] `promote-storefronts.yml` -> Josh production env-approval.

### Teardown (Thu 2026-09-24 scheduled / 2026-09-29 actual)
- [x] `bash scripts/qa-l3-teardown.sh compute` — tracked in **GOL-2336**.
- [x] Confirm survivors; verify re-up at **Train #2: up Mon 2026-10-05, promote Wed
  10-07, teardown Thu 10-08** — the *Oct 5* week. Older copies said "Oct 6 week";
  that was wrong.

### Lessons Train #1 fed back into the template above
- **Nobody read the skip list**, so the promo spec silently skipped and no e2e
  asserted WV tax. Hence the skip-list line in Pre-promote.
- **The promote leg ran ad hoc**, which is how both of its footguns (the proxied
  `PROD_HOST` hang, and the exit-6 stale-ref timeout) were found live on a revenue
  box. Both are fixed; the ordered procedure now lives in
  `RUNBOOK-release-train.md` → "The Wednesday promote leg".

### Automation follow-ups (children of GOL-2324)
- **GOL-2326** — one-command / scheduled train-up (Mon) + train-teardown (Thu).
  **DONE:** `make train-up` / `make train-teardown` + `release-train-reminder.yml`.
- **GOL-2327** — App Platform apps leg in teardown. **Resolved as Option A: the
  apps are DESTROYED** (`digitalocean_app.tenant`), not parked and not scaled to
  zero. Consequence worth remembering: anything live-only on an app or the Odoo
  droplet — an un-vaulted env secret — is **gone** after teardown, and the
  publish-webhook guard covers applies, not destroys.
- **GOL-2328** — this manifest template.
