# Release Train Manifest

Every Grove **release train** (biweekly QA window → promote → teardown, EPIC
[GOL-2324]) ships a bundle: a specific set of `grove-sites` PRs, one pinned
`grove-odoo-modules` ref, a set of QA gate targets, a promote plan, and a
teardown plan. This document makes that bundle **explicit, structured, and
auditable per train** so it is never reconstructed from memory each fortnight.

## How to use this

1. **Open the next train's tracking issue** by copying the
   [Manifest template](#manifest-template) below **verbatim** into a new
   Paperclip issue (child of [GOL-2324]) — or open a GitHub issue from the
   `Release Train` issue form (`.github/ISSUE_TEMPLATE/release-train.yml`),
   which renders the same fields as a structured form.
2. **Fill every field.** A field you cannot fill yet is a gap to close before
   the train departs, not a field to delete. Use `TBD` + a note.
3. **Bind SHAs to reconcile PRs, not to memory.** For the `grove-odoo-modules`
   ref and storefront image tags, cite the reconcile PR that carries the
   authoritative pin (see [odoocker pin reconcile](#odoocker-pin--reconcile)).
   The PR is the source of truth; the manifest points at it.
4. **Get CEO / Founding-Engineer sign-off** on the filled manifest before the
   QA-up date. Promote is additionally gated on **Josh's environment approval**
   on the production deploy.

See [`docs/RELEASE.md`](RELEASE.md) for tag/deploy mechanics and
[`docs/RUNBOOK-qa-test-data-cleanup.md`](RUNBOOK-qa-test-data-cleanup.md) for
the QA cleanup step.

---

## Manifest template

> Copy everything between the rules into the new train issue. Replace every
> `‹…›` placeholder. Keep the checkboxes — they are the go/no-go gate.

<!-- ──────────────── COPY FROM HERE ──────────────── -->

### Train #‹N› — week of ‹YYYY-MM-DD›

| Field | Value |
|-------|-------|
| **Train #** | ‹N› |
| **Week** | ‹YYYY-MM-DD› (Monday of the QA window) |
| **QA-up date** | ‹YYYY-MM-DD› — `make qa-l3-up` |
| **Promote date** | ‹YYYY-MM-DD› — prod deploy, **Josh env-approval required** |
| **Teardown date** | ‹YYYY-MM-DD› — `make qa-l3-teardown` (Option A: compute only) |
| **Parent EPIC** | [GOL-2324] |

#### Bundle — `grove-sites` PRs
<!-- One row per PR going out on this train. Image tag = the ghcr.io tag CI
     published for that PR head, which the odoocker storefront pin will point at. -->
| grove-sites PR | Title / purpose | Storefront image tag |
|----------------|-----------------|----------------------|
| #‹NNN› | ‹what it ships› | ‹short-sha› |

#### Bundle — `grove-odoo-modules` ref
- **Ref (SHA):** `‹40-char-sha›`  (bound by reconcile PR ‹odoocker #NNN›)
- **Delta vs current prod pin:** ‹short-sha…short-sha, N commits — or "default catch-up only"›

#### QA gate targets (all must be green before promote)
- [ ] `test:e2e:gate` — Playwright E2E gate (grove-sites) green on the QA L3 env
- [ ] `@stripe` — Stripe-tagged checkout suite green
- [ ] `make qa-l3-seed-e2e` ran (E2E inventory fixture seeded, idempotent)
- [ ] **`qa-test-data-cleanup`** ran after QA — `make qa-test-data-cleanup`
      (DRY-RUN reviewed) → `make qa-test-data-cleanup-apply` (surgical)

#### Promote plan
- [ ] odoocker storefront pin bump PR(s): ‹odoocker #NNN›
- [ ] odoocker `custom_modules_ref` reconcile PR(s) (if modules moved): ‹odoocker #NNN›
- [ ] Production deploy — `make tf-apply env=production CONFIRM=yes`
      / `doctl … create-deployment` per [`RELEASE.md`](RELEASE.md)
- [ ] **Josh environment approval** recorded on the prod deploy
- [ ] Rollback ref noted (previous prod pin): `‹short-sha›`

#### Teardown plan
- [ ] ‹YYYY-MM-DD› — `make qa-l3-teardown` (compute only; PG data / DNS / certs survive — [GOL-2327])
- [ ] Confirmed volumes / Managed PG / DNS zone **not** destroyed (Option A, not `-all`)

#### Re-up verification (next train)
- [ ] Next train (week of ‹YYYY-MM-DD›) `make qa-l3-up` brings QA back clean
      from cloud-init alone (proves teardown was reproducible, not a snowflake loss)

<!-- ──────────────── COPY TO HERE ──────────────── -->

---

## Train #1 — worked example (week of 2026-09-22)

Seeded from the [GOL-2324] EPIC body as the reference train. Values that are
bound by a reconcile PR cite that PR; confirm the exact SHA against the linked
PR at promote time.

### Train #1 — week of 2026-09-22

| Field | Value |
|-------|-------|
| **Train #** | 1 |
| **Week** | 2026-09-22 |
| **QA-up date** | 2026-09-22 — `make qa-l3-up` |
| **Promote date** | **2026-09-24 (Wed)** — prod deploy, Josh env-approval required |
| **Teardown date** | **2026-09-25 (Thu)** — `make qa-l3-teardown` (Option A: compute only) |
| **Parent EPIC** | [GOL-2324] |

#### Bundle — `grove-sites` PRs
| grove-sites PR | Title / purpose | Storefront image tag |
|----------------|-----------------|----------------------|
| grove-sites #758 | RCE-fix dependency bump (storefront) | `68f4e5c5` |

Storefront pin reconcile: [GOL-2316] (odoocker #666) advanced the prod
storefront tags `393b7696` → `68f4e5c5` (= live grove-sites #758 head),
default-catch-up, `custom_modules_ref` untouched.

#### Bundle — `grove-odoo-modules` ref
- **Ref (SHA):** bound by the modules reconcile PR — default catch-up to the
  proven prod-live rev. Confirm against the reconcile PR at promote
  ([GOL-2307] #660 advanced default catch-up `22fbb71` → `0b36ecfa`;
  [GOL-2273] #650 reconciled the three pins on main).
- **Delta vs current prod pin:** default catch-up only — no money-logic pin
  advance on Train #1 (prod modules remain board-gated, [GOL-1791]).

#### QA gate targets
- [ ] `test:e2e:gate` green on QA L3
- [ ] `@stripe` checkout suite green
- [x] `make qa-l3-seed-e2e` (idempotent E2E inventory fixture)
- [ ] `make qa-test-data-cleanup` (DRY-RUN) → `…-apply` after QA

#### Promote plan
- Storefront pin bump: odoocker #666 ([GOL-2316])
- Modules reconcile (if moved): odoocker #660 ([GOL-2307]) / #650 ([GOL-2273])
- Prod deploy Wed 2026-09-24 — **Josh env-approval required**
- Rollback ref: previous prod storefront tag `393b7696`

#### Teardown plan
- 2026-09-25 — `make qa-l3-teardown` (compute only; Option A DESTROY, [GOL-2327])
- Verify Managed PG data / DNS / certs survive (NOT `qa-l3-teardown-all`)

#### Re-up verification
- Next train **week of 2026-10-06** `make qa-l3-up` brings QA back clean from
  cloud-init — first re-up proving the 09-25 teardown was reproducible.

---

## odoocker pin / reconcile

The odoocker stack pins two things the train advances:

- **Storefront image tags** — the ghcr.io tags for the hub + 3 tenant
  storefronts, bumped to the grove-sites PR heads in the bundle.
- **`custom_modules_ref`** — the `grove-odoo-modules` SHA git-sync deploys.

Both live in `infra/terraform/environments/production/`. A **reconcile PR**
edits the pin to match the proven prod-live rev; `merge ≠ deploy` (prod uses
`ignore_changes` / `deploy_on_push=OFF`, so a plain apply is a no-op —
promotion is a deliberate `-replace` / `create-deployment`, see [GOL-1304]).
Cite the reconcile PR in the manifest so the authoritative SHA is one click
away and auditable.

## Related automation

- **[GOL-2326]** — `make train-up` / `train-down` aliases + Discord
  reminder-only workflow (reminders, not CI destroy).
- **[GOL-2327]** — App Platform teardown leg (Option A destroy).
- **[GOL-2324]** — parent EPIC (train cadence, gate schedule).

<!-- Issue links (Paperclip GOL-####); rendered as plain text on GitHub. -->
[GOL-2324]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2324
[GOL-2326]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2326
[GOL-2327]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2327
[GOL-2316]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2316
[GOL-2307]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2307
[GOL-2273]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-2273
[GOL-1791]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-1791
[GOL-1304]: https://paperclip.gatheringatthegrove.com/GOL/issues/GOL-1304
