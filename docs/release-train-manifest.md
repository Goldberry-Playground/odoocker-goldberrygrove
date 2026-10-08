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

---

## Train #2 — week of 2026-10-05 (system of record, frozen 2026-10-02)

Manifest issue: **GOL-2584**. Ratified by Josh 2026-09-29; the Stripe Tax item
restated by Josh on freeze day (2026-10-02) — see the ruling below. Every SHA,
version and PR state in this section was read back from the GitHub API at the
freeze, not copied from the issue text, because the issue text had drifted (it
still named a gom HEAD and a module version that two merges had already moved).

- **Merge freeze:** Fri 2026-10-02 EOD ET. Later merges ride Train #3 (up Mon
  2026-10-19), except P0/P1 money-path hotfixes, which bypass the train per canon.
- **QA-up date (Mon):** 2026-10-05 (`make train-up` — gated on `make train-preflight`)
- **Promote date (Wed):** 2026-10-07 (Josh approves the production environment)
- **Teardown date (Thu):** 2026-10-08 (`make train-teardown`, compute only)

### Bundle

- **grove-odoo-modules ref (backend):** `main` @ **`5ccd9b62a8`** —
  `grove_headless` **19.0.1.58.0**, **18 commits ahead** of the pin production is
  serving (`bbb580e0`, 1.55.2). Money/compliance content in this delta:
  - **#284 Stripe Tax on headless checkout (GOL-2568)** — ships **INERT**, see below.
  - #277/#283 pool-aware `in_stock` → `available` (**must promote together with
    grove-sites' storefront pin**; the API rename and the consumer land as a pair).
  - #300 `contact.phone` required on every order.
  - #299 shop departments — the only backward-compat risk in the bundle; audited
    against prod's live category tree, and only category 6's re-slug
    (`food-forest-packages` → `guilds`) breaks. `/shop` pills, filters, PDPs and
    checkout are unaffected (GOL-2808, odoocker#801).
  - #301 5-Tree Native Bundle into Guilds; #302 SEO title/description on the
    product serializers; #290 phantom BoMs; #271/#272/#286 QA gate fixtures.
- **grove-sites storefront image pin:** `d4d248ef04d94f2a64d16010eff6146283e0968a`
  (landed by odoocker#823; **supersedes** the older `ba36ff2f` pin in the closed
  #816 — do not resurrect it).

### Stripe Tax — ships INERT (Josh ruling, 2026-10-02)

The 2026-09-29 manifest said "Stripe Tax LIVE, and if a gate is unmet at freeze
it drops to Train #3". That was unsatisfiable as written: Gates 3 and 4 need a
live QA, and QA does not exist until Mon 10-05, so they could never be met by a
Friday freeze. Josh's restatement:

> gom#284 rides Train #2 with every `GROVE_STRIPE_TAX_*` flag OFF. The flag flips
> at the Wed 10-07 promote **only if Gates 3 and 4 pass on QA Mon–Tue**. If either
> is unmet by promote, the code still ships inert and the flip moves to Train #3.
> No further ruling needed.

Flag OFF is today's behaviour (Odoo's WV tax line), so "inert" is a true no-op.
Verified in merged code at `5ccd9b62`, not taken on trust:
`_stripe_tax_enabled` returns False for unset/empty/non-truthy **and** for an
order with no resolvable tenant, and all four Stripe Tax call sites (session
`automatic_tax`, `_build_stripe_line_items`, the webhook write-back, the
ship-time settlement calc) are behind it. Gate 1 (Odoo-19 install green), Gate 2
(defaults OFF) and Gate 5 (version collision gone — 1.58.0) are **met**.

**Gates 3 and 4 are now mechanical:** `make stripe-tax-gate` → see
`scripts/stripe-tax-gate.py`. Exit 1 means the flip is not cleared, which per
the ruling needs no escalation — the code is already inert. Run order, because
the two gates need opposite flag states:

1. `bash scripts/stripe-tax-gate.sh --gate 3 --json-out /tmp/gate3.json` (flag
   OFF, which is how QA comes up — the Terraform tenant set defaults to empty)
2. flip nursery ON on the QA droplet — Path A in `docs/RUNBOOK-module-upgrade.md`
3. `bash scripts/stripe-tax-gate.sh --gate 4 --merge /tmp/gate3.json --json-out /tmp/stripe-tax-verdict.json`
4. `bash scripts/qa-test-data-cleanup.sh --apply` before recording the verdict

**Blocker on Gate 4a:** GOL-2574 (Stripe **test-mode** WV registration). Stripe
Tax settings are per mode and do not inherit from live, so until that exists Gate
4a returns `NO_TAX` and the flip is correctly refused.

#### Gate 4c — added at the freeze, and the reason the prose gate was not enough

The written gate asserts two address classes on the **charge-in-full** path.
`checkout_session` computes `automatic_tax = tax_enabled and not is_deposit`, so
a **deposit** order keeps Stripe Tax off at checkout and defers all tax to the
ship-time settlement's `/v1/tax/calculations`. Train #2 promotes **Oct 07**; the
GOL-2233 season cutover is **Oct 15**, and from Oct 16 every shipped order
containing bareroot takes the flat $10 deposit (potted-only and farm-pickup keep
charging in full). Bareroot is the dormant-season product, so within nine days of
a flip the deposit path carries the dominant shape of nursery orders — a path the
two prose cases never touch. Gate 4c asserts it: deposit session carries **no**
`automatic_tax`, charges exactly the flat $10, and reports $0 tax.

Two items this surfaces for Train #3, neither blocking the inert ship:

- The settlement-time Stripe Tax calc is **best-effort** — a gateway failure is
  caught, logged at WARNING, and silently falls back to Odoo's `amount_total`.
  Safe (the customer is still taxed) but invisible: there is no Discord alert on
  a money-path fallback. Worth a watcher before the flag ever goes on in prod.
- The settlement leg of the deposit path had no automated gate; Gate 4c
  proves tax stands down at checkout, not that Stripe Tax computes the balance
  correctly at ship. **Now Gate 4d (GOL-2910):** a deposit order is driven to
  the ship event on QA and the gate asserts `grove_stripe_tax_amount` equals the
  recorded breakdown's `tax_amount_exclusive` at ~6% of the shipped base, the
  breakdown names WV, and the captured balance is `base + tax − $10`. A
  settlement that fell back to Odoo tax is `STRIPE_TAX_NOT_USED` (red). 4d is in
  the Gate 4 set, so `flip_allowed` is false without it. Needs
  `QA_ODOO_RPC_LOGIN/PASSWORD` (`scripts/stripe-tax-gate.env.op`).
- The fallback itself now alerts: grove-odoo-modules#331 posts a
  `Stripe Tax FALLBACK` Discord alert + order chatter on a failed ship-time calc.

### Gate (on QA, Mon–Wed)

- [ ] `make train-preflight` clean (it gates `make train-up`)
- [ ] `bash scripts/qa-test-data-cleanup.sh --apply` run BEFORE recording verdicts
- [ ] `scripts/qa-module-upgrade.sh grove_headless`, THEN dispatch `e2e-nursery.yml`
      as the **last** step (Train #1 lesson: a gate dispatched before the upgrade
      tests the old code)
- [ ] staleness checked in **both** repos before trusting a green
- [ ] skip list READ — the promo, WV-tax and Stripe-tax specs must execute, not skip
- [ ] `make stripe-tax-gate` verdict captured (`flip_allowed` true/false)
- **Verdict:** PASS / FAIL @ <run link>

### Promote (Wed 10-07 — Josh approves the production environment)

- [x] Reconcile PRs deduped — winner **#823** (`d4d248ef`), closed: #816
- [ ] odoocker `custom_modules_ref` bump + default-catch-up reconcile → `5ccd9b62`
      (or main HEAD at promote, re-verified then — this SHA moved twice in the
      48h before the freeze)
- [ ] `promote-storefronts.yml` green with Josh env-approval
- [ ] **Separate named step, only if `flip_allowed`:** `GROVE_STRIPE_TAX_NURSERY=1`
      via Path A, verified at the **process** (`docker compose exec -T odoo
      printenv`), never from the files. Prod's `user_data` is in `ignore_changes`,
      so no `terraform apply` injects it.
- **Prod verified:** JSON-RPC version bracket + health check link

### Teardown (Thu 10-08)

- [ ] `make train-teardown` (compute only — **never** `all`)
- [ ] Survivors confirmed: Managed PG, filestore + LE-cert volumes, reserved IP,
      DNS zone + CF delegation. The QA obs droplet is retired; **no obs exemption**.

### Pre-train prerequisites — state at freeze

| Prereq | State |
|--------|-------|
| `custom_modules_ref` not lagging prod | **done** — reconciled to `bbb580e0` (#785/#786) |
| Stripe Tax flag wiring in QA + prod TF | **#805** — approved by Josh at head, all 34 checks green, merge-queued at the freeze. Prod half is a provable no-op (`ignore_changes = [user_data]`); the QA half activates on the Mon `train-up` rebuild with the tenant set empty = flags OFF |
| QA PG firewall survives teardown | **#753**, with Josh. **#754 closed as the duplicate** (conflicted with #753 on `qa-app-platform/main.tf`); its regression test `scripts/test_qa_pg_firewall_independence.py` is to be re-landed on top of #753 after the window — branch kept, not deleted |
| GOL-2436 volume program on PROD | done — both prod volumes attached (verify with `lsblk`/DO Volumes, not a PR badge) |
| Terraform state locking verified | **NOT MET.** `use_lockfile` is a **no-op on DO Spaces** — zero mutual exclusion in all 9 envs including production (GOL-2755 / #790 advisory guard; board decision pending). `train-preflight` fails closed here; the documented escape is `TRAIN_PREFLIGHT_ACK_LOCK=1`, which means "one operator, serialized by hand" |

### Out — Train #3

gom#279/#280 (listing-content UX), gom#281 (shipping-rate drift; the Pirate Ship
probe is still locked out, GOL-2605), gom#303/#304 and grove-sites #892 (the
`guilds` re-slug consumer) if they land after the freeze. RUM (GOL-2331) is
descoped from the Obs Phase 2 10-10 deadline by Josh; synthetics, Keep→Discord,
OTLP ingest and Beyla still target 10-10 and are infra, not train-bound.
