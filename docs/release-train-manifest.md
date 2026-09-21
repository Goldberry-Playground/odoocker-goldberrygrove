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
| Mon | `make qa-l3-up` (train-up) | Josh approval on spend (GOL-2326) |
| Mon–Wed | Bundle + gate on QA | `test:e2e:gate` + `@stripe`, cleanup first |
| Wed | Promote: pin bump + reconcile + `promote-storefronts.yml` | Josh production env-approval |
| Thu | `bash scripts/qa-l3-teardown.sh compute` (teardown) | keep PG/filestore/reserved-IP/DNS; NEVER `all` |

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
- [ ] `test:e2e:gate` green
- [ ] `@stripe` suite green
- **Verdict:** PASS / FAIL @ <commit/run link>

#### Promote (Wed — Josh approves production environment)
- [ ] Reconcile PRs deduped (no competing storefront pins) — winner: #___, closed: #___
- [ ] odoocker pin bump + default-catch-up reconcile of `custom_modules_ref` -> backend SHA
- [ ] `promote-storefronts.yml` run green with Josh env-approval
- **Storefront image pinned:** <SHA>
- **Prod verified:** rate probe / health-check link

#### Teardown (Thu — first-of-cycle compute-down)
- [ ] `bash scripts/qa-l3-teardown.sh compute`
- [ ] Survivors confirmed present (Managed PG, filestore + LE-cert volumes, reserved IP, DNS zone + CF delegation)
- [ ] Re-up verified at NEXT train (`make qa-l3-up` -> health green)
```

---

## Train #1 — week of 2026-09-22 (worked example)

QA already up since 2026-09-08.

- **QA-up date:** 2026-09-08 (pre-cadence; already up)
- **Promote date (Wed):** 2026-09-24
- **Teardown date (Thu):** 2026-09-25  ← **first-ever teardown**

### Bundle
- **grove-sites PRs:**
  - #737 — FL mirror. Resolve per the **GOL-2235 semantic addendum**: `zone_6` = FL
    rates, **delete stale `zone_7`**, map FL.
  - #770 — estimator zone-map fix.
  - #750 — PDP copy (verify on QA).
  - #748 — gate (verify on QA).
- **grove-odoo-modules ref:** `main HEAD` — carries the **full GOL-2132 compliance
  stack (#214 / #215 / #217)**, so **FL ships in prod with this train, correctly
  substituted**. (Prod modules currently frozen @ `8accb94` per GOL-1791; this
  train's pin bump is the gated money release — confirm delta intended with CEO.)

### Gate
- [ ] cleanup, `test:e2e:gate`, `@stripe` — tracked in **GOL-2334**.

### Promote (Wed 2026-09-24)
- [ ] **Dedupe #666 vs #667 FIRST** — both open, both `mergeable_state: blocked`,
  competing storefront pins:
  - #666 -> `68f4e5c5` (GOL-2316, = live grove-sites #758 RCE bump)
  - #667 -> `578b828b`
  Pick the SHA matching the Train #1 bundle build, close the loser. Tracked in **GOL-2335**.
- [ ] odoocker pin bump + default-catch-up reconcile -> modules `main HEAD`.
- [ ] `promote-storefronts.yml` -> Josh production env-approval.

### Teardown (Thu 2026-09-25)
- [ ] `bash scripts/qa-l3-teardown.sh compute` — tracked in **GOL-2336**.
- [ ] Confirm survivors; verify re-up at Train #2 (Oct 6 week).

### Automation follow-ups (children of GOL-2324)
- **GOL-2326** — one-command / scheduled train-up (Mon) + train-teardown (Thu).
- **GOL-2327** — App Platform apps park/scale leg in teardown.
- **GOL-2328** — this manifest template.
