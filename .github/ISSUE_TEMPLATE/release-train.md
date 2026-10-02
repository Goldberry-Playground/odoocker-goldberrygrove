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

  WHO DOES WHAT
  - Josh RUNS both make legs locally (`make train-up` Mon, `make train-teardown`
    Thu). They resolve op:// refs out of the "Goldberry Grove - Admin" vault,
    which no agent service account can read.
  - Terra CONDUCTS: bundle, this manifest, gate reading, promote PRs, verification.

  INVARIANTS (don't relearn these the hard way — see docs/ + CLAUDE.md):
  - "Merged to main" ≠ "applied". qa-app-platform / production applies are
    MANUAL. Verify with lsblk / DO volumes / doctl, not the PR badge.
  - PROMOTE IS TWO LEGS, and only Leg A is a workflow:
      Leg A storefronts = `.github/workflows/promote-storefronts.yml`
        (production-gated dispatch). A BLANK target_sha resolves to the newest
        grove-sites main commit with a GREEN docker.yml run and verifies all four
        ghcr.io/goldberry-playground/grove-*:<sha> tags BEFORE the gate (#821,
        merged 2026-10-01). Blank does NOT mean "main HEAD".
      Leg B modules pin = `scripts/prod-modules-promote.sh` with
        TARGET_REF=<40-char sha> CONFIRM=PROMOTE, then `reconcile-modules-pin.yml`
        to converge the committed tfvars default onto the live pin. It is NOT
        "tag -> release.yml" and NOT an SSH `docker compose pull`. Leg A hard-aborts
        if a bump ever touches custom_modules_ref. Run Leg B ONCE — a failure is
        real, there is no idempotent re-run.
    A green `terraform apply` is NOT evidence of a deploy for either leg.
  - SOAK THE MIGRATIONS ON QA BEFORE LEG B. `scripts/qa-module-upgrade.sh
    grove_headless` against QA, and confirm the effects in the DATABASE, not the
    log. No soak -> the module half does not promote; it rides the next train.
  - e2e-nursery.yml is the LAST pre-promote step — after the final merge AND after
    the QA soak — and you READ THE SKIP LIST, not just the green. The promo,
    WV-tax and Stripe-Tax specs must EXECUTE. Train #1 shipped with the promo
    spec silently skipped.
  - PER-TENANT ENV FLAGS SHIP INERT. A flag like GROVE_STRIPE_TAX_{TENANT} lands
    OFF and is flipped at the Wed promote ONLY if its QA gates passed; otherwise
    it rides the next train (a planned outcome, not an incident). Rollback = flag
    off. prod's user_data is in ignore_changes, so no `terraform apply` activates
    it — see docs/RUNBOOK-module-upgrade.md.
  - grove-odoo-modules ref MUST be an immutable 40-char SHA, never a moving tag.
  - Every promote needs Josh's GitHub `production` environment approval.
  - TEARDOWN tears down COMPUTE ONLY. qa-l3-teardown.sh keeps PG / filestore /
    reserved-IP / DNS. NEVER run the DNS script in a teardown.
  - The QA App Platform apps are DESTROYED each teardown (GOL-2327 Option A) —
    NOT parked, NOT scaled to zero. Anything live-only on an app or the droplet
    (an un-vaulted secret) is GONE after teardown.
  - NOTHING IS EXEMPT FROM TEARDOWN. grove-qa-l3-obs was retired 2026-09-29
    (ADR-010) along with its QA_L3_TEARDOWN_OBS exemption.
-->

## Train identity

| Field | Value |
|---|---|
| **Train #** | `<fill: NN>` |
| **Week** | `<fill: e.g. week of 2026-10-05>` |
| **QA-up date** | `<fill: YYYY-MM-DD>` — QA env stood up / refreshed |
| **Promote date** | `<fill: YYYY-MM-DD (Wed)>` — prod cutover |
| **Teardown date** | `<fill: YYYY-MM-DD (Thu)>` — QA env torn down |
| **Conductor (DevOps)** | @<fill> — runs the bundle/manifest/gate/promote PRs |
| **Leg operator (make train-up / train-teardown)** | Josh (local; Admin-vault creds) |
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
- [ ] **Skip list read**, not just the green — the promo, WV-tax and Stripe-Tax
      specs **executed**. Paste the skipped-spec list: `<fill>`
- [ ] Staleness checked in **both** repos (grove-sites AND grove-odoo-modules) —
      a green run against a stale head is not a pass.

> Record the run link(s):
> - e2e:gate: `<fill: run URL>`
> - @stripe: `<fill: run URL>`

## Pre-promote — required, in this order

- [ ] **QA `-u` soak of the module migrations.** `scripts/qa-module-upgrade.sh
      grove_headless` against QA with the SAME SHA Leg B will get. Confirm the
      effects in the **database** (`ir_module_module.latest_version`, plus the
      tables/rows the migration creates) — **not** the log line. A `-u` on a DB
      already past the migration's version legitimately *skips* it, so the log
      can be clean and the change absent (the WV-tax bind is the standing
      example). **No soak ⇒ the module half does not promote; it rides the next
      train.**
  - soak run / DB evidence: `<fill>`
- [ ] **LAST step before promote: re-dispatch `e2e-nursery.yml` on `main`** —
      after the final merge **and** after the soak — and read the skip list.
  - run link + skipped specs: `<fill>`

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
- [ ] **Leg A — storefront roll** via `promote-storefronts.yml`
      (`workflow_dispatch`, `production`-gated). A **blank `target_sha`** resolves
      to the newest grove-sites `main` commit with a **green `docker.yml`** run and
      verifies all four `ghcr.io/goldberry-playground/grove-*:<sha>` tags before the
      gate (#821) — blank is **not** "main HEAD". Fallback only if the workflow is
      unavailable: single-fire `doctl apps create-deployment` per app.
      - resolved `target_sha`: `<fill>`
      - grove-hub-prod (`d5fa7795…`): `<fill: deployment id>`
      - grove-nursery-prod (`b9e0d2a6…`): `<fill>`
      - grove-goldberry-prod (`3da0b924…`): `<fill>`
      - grove-ggg-prod (`30c2a739…`): `<fill>`
- [ ] **Leg B — Odoo modules pin** (only if a new module ref is in the bundle):
      `TARGET_REF=<40-char sha> CONFIRM=PROMOTE scripts/prod-modules-promote.sh`,
      then `reconcile-modules-pin.yml` to converge the committed tfvars default
      onto the live pin. **NOT** "tag → `release.yml`", **NOT** an SSH
      `docker compose pull`. Run it **once** — a failure is real, there is no
      idempotent re-run. Runbook: `docs/RUNBOOK-module-upgrade.md` →
      "Promoting the prod modules pin -- Leg B".
      - pre-flight output (read-only) reviewed: `<fill>`
      - live `CUSTOM_MODULES_REF` after promote: `<fill>` = grove_headless `<fill>`
      - reconcile PR: `<fill: #NNN>`
- [ ] **Named flag step** — only if this train carries a per-tenant env flag
      (e.g. `GROVE_STRIPE_TAX_{TENANT}`, GOL-2568). It shipped **OFF**. Flip it
      here **only if its QA gates passed**; if any gate is unmet, inconclusive, or
      did not run, **do not flip** — it rides the next train (planned outcome, no
      escalation). **Rollback = flag off.** Path A env-file upsert +
      `up -d --force-recreate --no-deps odoo`; a plain restart does nothing.
      - gates passed? `<fill: yes/no + evidence>`  → flipped? `<fill>`

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
- [ ] QA App Platform apps **destroyed** (`digitalocean_app.tenant`) — GOL-2327
      resolved as **Option A: destroy**, not park and not scale-to-zero. Anything
      live-only on an app or the Odoo droplet (an un-vaulted secret) is **gone**
      after this; the publish-webhook guard covers applies, not destroys.
- [ ] Nothing claimed an exemption. `grove-qa-l3-obs` and `QA_L3_TEARDOWN_OBS`
      were retired 2026-09-29 (ADR-010); `grove-obs` lives in a different state
      and is unreachable from this teardown under any flag.
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
| **Promote date** | 2026-09-23 (Wed) |
| **Teardown date** | 2026-09-24 (Thu) scheduled — **actually ran 2026-09-29**, five days late. That slip is why `release-train-reminder.yml` exists. |
| **Parent EPIC** | GOL-2324 |

**Bundle — grove-sites PRs** (as actually shipped)
- `#783` — FL mirror, merged 2026-09-21 (`zone_5`, per GOL-2345 / GOL-2235).
  ⚠️ Earlier copies of this example listed `#737`; that PR was **closed unmerged**
  (draft, pre-addendum `zone_6`/`zone_7` shape) and never shipped.
- `#770` — estimator zone-map fix (merged 2026-09-21).
- `#750` — PDP copy (merged 2026-09-14).
- `#748` — gate (merged 2026-09-16).

**Bundle — grove-odoo-modules ref:** `main` HEAD — carries the FULL GOL-2132
compliance stack (`#214`/`#215`/`#217`), so FL ships in prod with this train,
correctly substituted.

**Gate targets:** `test:e2e:gate` + `@stripe` on QA; `qa-test-data-cleanup`
before verdicts. **Lesson learned:** nobody read the skip list, so Train #1
promoted with the promo spec silently skipped and no e2e asserting WV tax. Hence
the skip-list checkbox above.

**Promote (Wed 2026-09-23):** Leg A `promote-storefronts.yml` (Josh approves the
GitHub `production` environment) + Leg B `scripts/prod-modules-promote.sh` then
the default-catch-up reconcile (**dedupe the competing `#666`/`#667` reconciles
first**).

**Teardown (Thu 2026-09-24 scheduled / 2026-09-29 actual) — first ever:**
`qa-l3-teardown.sh` compute only — keep PG / filestore / reserved-IP / DNS;
**NEVER** the DNS script. Re-up verified at the next train, **Train #2: up Mon
2026-10-05, promote Wed 10-07, teardown Thu 10-08** (the *Oct 5* week — "Oct 6
week" in older copies was wrong).

</details>
