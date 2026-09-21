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

  CADENCE (canon: vault [[Software/Grove Release Train (QA Cadence)]], CEO-ratified
  2026-09-20). QA compute runs ONLY inside train windows:
    biweekly Mon qa-l3-up → Mon–Wed bundle+gate → Wed promote (Josh env-approval)
    → Thu qa-l3-teardown.sh compute.  (~70% QA compute reduction.)

  INVARIANTS (don't relearn these the hard way — see docs/ + CLAUDE.md):
  - "Merged to main" ≠ "applied". qa-app-platform / production applies are
    MANUAL. Verify with lsblk / DO volumes / doctl, not the PR badge.
  - Storefront promote = pin bump (this repo TF) + per-app `doctl apps
    create-deployment`. A green `terraform apply` is NOT evidence of a deploy.
    Canonical mechanism: grove-sites `scripts/lib/do-app-redeploy.sh` /
    this repo's `.github/workflows/promote-storefronts.yml` (production-gated).
  - grove-odoo-modules ref MUST be an immutable 40-char SHA, never a moving tag.
  - Every promote needs Josh's GitHub `production` environment approval.
  - TEARDOWN tears down COMPUTE ONLY. qa-l3-teardown.sh keeps PG / filestore /
    reserved-IP / DNS. NEVER run the DNS script in a teardown.
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

## Teardown (`<teardown date>`) — COMPUTE ONLY

- [ ] `qa-l3-teardown.sh` run — tears down QA **compute** only.
      **Keeps** PG / filestore / reserved-IP / DNS. **NEVER** run the DNS script.
- [ ] QA App Platform apps parked/scaled down (they otherwise run 24/7 — see
      GOL-2324 automation child (b)).
- [ ] No idle droplets / preview resources still billing (verify via `doctl` / DO console).
- [ ] **Re-up verification:** QA stands back up from code alone at the **next**
      train's Mon qa-l3-up (reproducibility check — no snowflake).
  - re-up check: `<fill: next-train date + result>`

## Sign-off

- [ ] Conductor (DevOps) — bundle applied + verified as recorded above.
- [ ] App reviewer (Eng) — frontend/module bundle reviewed.
- [ ] CEO/board — promote authorized (money-flow / prod-cred gate).

---

<details>
<summary><b>Worked example — Train #1 (immediate; QA up since 2026-09-08)</b> · seed reference from GOL-2324, do not edit</summary>

> Seeded verbatim from the GOL-2324 EPIC body (CEO-ratified 2026-09-20) as the
> first worked bundle. Train #1's QA was already up since 09-08, so it has no
> distinct Mon qa-l3-up — the biweekly Mon-up cadence starts from Train #2.

| Field | Value |
|---|---|
| **Train #** | 1 |
| **Week** | week of 2026-09-21 (QA already up since 2026-09-08) |
| **QA-up date** | 2026-09-08 (pre-cadence; QA is the Grove system-of-record) |
| **Promote date** | 2026-09-24 (Wed) |
| **Teardown date** | 2026-09-25 (Thu) — **first ever** teardown |
| **Parent EPIC** | GOL-2324 |

**Bundle — grove-sites PRs**
- `#737` — FL mirror (resolve per the GOL-2235 semantic addendum: `zone_6`=FL rates, delete stale `zone_7`, map FL).
- `#770` — estimator zone-map fix.
- verify `#750` (PDP copy) + `#748` (gate) on QA.

**Bundle — grove-odoo-modules ref:** `main` HEAD — carries the FULL GOL-2132
compliance stack (`#214`/`#215`/`#217`), so FL ships in prod with this train,
correctly substituted.

**Gate targets:** `test:e2e:gate` + `@stripe` on QA; `qa-test-data-cleanup`
before verdicts.

**Promote (Wed 2026-09-24):** odoocker pin bump + default-catch-up reconcile
(**dedupe the competing `#666`/`#667` reconciles first**) + `promote-storefronts.yml`
(Josh approves the GitHub `production` environment).

**Teardown (Thu 2026-09-25) — first ever:** `qa-l3-teardown.sh` compute only —
keep PG / filestore / reserved-IP / DNS; **NEVER** the DNS script. Verify re-up
works at the NEXT train (Oct 6 week).

</details>
