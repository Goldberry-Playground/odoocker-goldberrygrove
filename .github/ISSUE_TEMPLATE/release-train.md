---
name: "🚂 Release Train"
about: "Biweekly QA-window + teardown train. One issue per train; the bundle is explicit and auditable — not reconstructed from memory each fortnight."
title: "EPIC: Grove Release Train #NN — QA-up MON DD, promote WED DD, teardown THU DD"
labels: ["release-train"]
assignees: []
---

<!--
  GROVE RELEASE TRAIN MANIFEST  (GOL-2324 automation / GOL-2328)

  HOW TO USE
  1. Copy this whole body into a new train issue (parent = GOL-2324 EPIC).
  2. Fill every `<fill>` and check boxes as each step lands.
  3. Keep it in sync with reality — this manifest IS the audit record for the
     train. A step that happened but is unchecked here reads as "not done".
  4. A fully worked example (Train #1) is in the collapsed section at the very
     bottom — use it as the reference for what "good" looks like.

  INVARIANTS (don't relearn these the hard way — see docs/ + CLAUDE.md):
  - "Merged to main" ≠ "applied". qa-app-platform / production applies are
    MANUAL. Verify with lsblk / DO volumes / doctl, not the PR badge.
  - Storefront promote = pin bump (this repo TF) + per-app `doctl apps
    create-deployment`. A green `terraform apply` is NOT evidence of a deploy.
    Canonical mechanism: grove-sites `scripts/lib/do-app-redeploy.sh` /
    this repo's `.github/workflows/promote-storefronts.yml` (production-gated).
  - grove-odoo-modules ref MUST be an immutable 40-char SHA, never a moving tag.
  - Every promote needs Josh's GitHub `production` environment approval.
-->

## Train identity

| Field | Value |
|---|---|
| **Train #** | `<fill: NN>` |
| **Week** | `<fill: e.g. week of 2026-10-06>` |
| **QA-up date** | `<fill: YYYY-MM-DD>` — QA env stood up / refreshed |
| **Promote date** | `<fill: YYYY-MM-DD (Wed)>` — prod cutover |
| **Teardown date** | `<fill: YYYY-MM-DD (Thu)>` — QA env torn down |
| **Conductor (DevOps)** | @<fill> |
| **App reviewer (Eng)** | @<fill> |
| **Parent EPIC** | GOL-2324 |

## Bundle — what ships on this train

### grove-sites PRs (frontend)
List every grove-sites PR that is bundled into this train's storefront images.

- [ ] `<fill: Goldberry-Playground/grove-sites#NNN — title>`
- [ ] `<fill: #NNN — title>`

**Resulting storefront image tag (40-char SHA):** `<fill>`
> The commit SHA published by grove-sites CI that both `hub_image_tag` and
> `tenant_image_tag` will be pinned to in
> `infra/terraform/environments/production/variables.tf`.

### grove-odoo-modules ref
- **Ref pinned (`custom_modules_ref`, 40-char SHA):** `<fill>`
- [ ] `<fill: Goldberry-Playground/grove-odoo-modules#NNN — title>` (each modules PR in the bundle)

## Gate targets (must be green before promote)

- [ ] `test:e2e:gate` green on the train's QA head
- [ ] `@stripe` (checkout / payment) suite green
- [ ] `<fill: any train-specific gate>`

> Record the run link(s):
> - e2e:gate: `<fill: run URL>`
> - @stripe: `<fill: run URL>`

## Pre-freeze / QA hygiene

- [ ] **qa-test-data-cleanup** run (dry-run reviewed, then apply) — clears
      synthetic canary orders / test carts without touching QA's real
      system-of-record data. Runbook: `docs/RUNBOOK-qa-test-data-cleanup.md`
      (`make qa-test-data-cleanup` → `make qa-test-data-cleanup-apply`).
  - cleanup run link / summary: `<fill>`

## Promote plan (prod cutover — `<promote date>`)

- [ ] **odoocker pin bump PR(s):** `<fill: Goldberry-Playground/odoocker-goldberrygrove#NNN>`
      — bumps `hub_image_tag` / `tenant_image_tag` (and/or `custom_modules_ref`)
      to the SHAs above. Reviewed + merged.
- [ ] **Reconcile PR(s)** (keep the three pins — modules, hub, tenant — in
      lockstep with prod-live): `<fill: #NNN>`
- [ ] **Josh `production` environment approval** granted (GitHub Environments
      gate / SHA-bound). Approver: @<fill>, at `<fill: time/link>`
- [ ] **Storefront roll executed** via `promote-storefronts.yml` **or** manual
      single-fire `doctl apps create-deployment` per app. Deployment IDs:
      - grove-hub-prod (`d5fa7795…`): `<fill: deployment id>`
      - grove-nursery-prod (`b9e0d2a6…`): `<fill>`
      - grove-goldberry-prod (`3da0b924…`): `<fill>`
      - grove-ggg-prod (`30c2a739…`): `<fill>`
- [ ] **Odoo compose stack** promoted if a new module ref is in the bundle
      (tag → `release.yml`, or targeted apply → SSH `docker compose pull/up`).

## Post-promote verification

- [ ] All four storefronts 200 + correct build fingerprint after roll
      (`gatheringatthegrove.com`, `atthegrovenursery.com`, `goldberrygrove.farm`,
      `woodworkingeorge.com`).
- [ ] Odoo prod health check green; git-sync landed the pinned modules ref.
- [ ] Prod pins in `variables.tf` match the running `active_deployment` SHAs
      (drift alarm quiet).
- Verification notes / links: `<fill>`

## Teardown (`<teardown date>`)

- [ ] QA droplet(s) / preview resources torn down (no idle droplets billing).
- [ ] QA volumes / snapshots handled per policy (durable data retained, ephemeral gone).
- [ ] **Re-up verification:** QA can be stood back up from code alone before
      next train's QA-up date (reproducibility check — no snowflake).
  - re-up check: `<fill: date + result>`

## Sign-off

- [ ] Conductor (DevOps) — bundle applied + verified as recorded above.
- [ ] App reviewer (Eng) — frontend/module bundle reviewed.
- [ ] CEO/board — promote authorized (money-flow / prod-cred gate).

---

<details>
<summary><b>Worked example — Train #1 (week of 2026-09-21)</b> · seed reference, do not edit</summary>

> Seeded from the GOL-2324 EPIC as the first worked bundle. Cross-check any
> values marked ⚠ against the canonical GOL-2324 body before reusing verbatim.

| Field | Value |
|---|---|
| **Train #** | 1 |
| **Week** | week of 2026-09-21 |
| **QA-up date** | ~2026-09-22 (QA env is the Grove system-of-record since 2026-07-09) |
| **Promote date** | 2026-09-24 (Wed) |
| **Teardown date** | 2026-09-25 (Thu) — first teardown |
| **Conductor (DevOps)** | DevOps (Terra) |
| **App reviewer (Eng)** | Engineering (Alice) |
| **Parent EPIC** | GOL-2324 |

**Bundle**
- grove-sites: RCE-bump build → storefront tag `68f4e5c5…` ⚠ (grove-sites#758, per GOL-2316 reconcile).
- grove-odoo-modules ref: prod-live main HEAD `0b36ecfa…` ⚠ (GOL-2307 catch-up).
- Reconcile PRs: odoocker #653 (reconcile automation, GOL-2286), #666 (storefront-tag reconcile, GOL-2316).

**Gate targets:** `test:e2e:gate` + `@stripe` green on QA head.

**Pre-freeze:** `qa-test-data-cleanup` before the 09-25 teardown
(`docs/RUNBOOK-qa-test-data-cleanup.md`).

**Promote (09-24):** pin bump → targeted `terraform apply` → per-app
`doctl apps create-deployment` (or `promote-storefronts.yml`), gated on Josh's
GitHub `production` environment approval (SHA-bound).

**Teardown (09-25):** tear down QA preview resources; verify QA re-ups from code
before Train #2 (week of 2026-10-06).

</details>
