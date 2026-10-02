# Runbook — custom-module upgrades on L3 Odoo (QA + production)

**Owner:** DevOps (Terra) · **Origin:** GOL-1009 · **Related:** GOL-746 (boot gate), GOL-987 (prod git-sync SHA-pin)

## TL;DR

Custom-module DDL now runs **automatically on deploy**. You should not need to
run anything by hand. The escape hatch is `scripts/module-upgrade.sh`.

## The problem this solves

Custom modules (`grove_headless` et al.) are delivered to the L3 droplets by the
`custom-modules-sync` git-sync sidecar, which writes `/workspace/current`. The
`qa` and `production` branches of `odoo/entrypoint.sh` boot with `--init=base`
and **no `--update`** — deliberately, so a `restart: unless-stopped` bounce does
not re-run migrations (see the branch comments + GOL-746).

The gap (found E2E-verifying GOL-1003 on QA): when git-sync advances to code that
adds a **new model**, the next boot loads that model into Odoo's Python registry
but **never creates its DB table**, because only an explicit `-u <module>` runs a
model's DDL. First touch of the new model then 500s:

```
psycopg2.errors.UndefinedTable: relation "grove_publish_event" does not exist
```

(`grove.publish.event` shipped in GOL-985; `grove_guide_ready` from an earlier PR
was fine because it had been upgraded before.)

## The durable fix (automatic)

`odoo/entrypoint.sh` runs a **revision-gated** upgrade pass at boot, before the
server starts:

```
odoo --init=base,$AUTO_UPGRADE_MODULES --update=$AUTO_UPGRADE_MODULES \
     --stop-after-init --no-http --workers=0 --without-demo=all
```

- **Opt-in** via `AUTO_UPGRADE_MODULES` (comma list), set to `grove_headless` in
  the qa (`environments/qa-app-platform/compose/docker-compose.qa.yml`) and
  production (`environments/production/compose/docker-compose.odoo.yml`) compose
  `environment:` blocks. Local / preview / testing don't set it → untouched.
- **Idempotent + rev-gated.** The last-upgraded revision (git-sync's per-commit
  worktree name = basename of the `/workspace/current` symlink target) is
  recorded in a marker on the **durable filestore volume**
  (`/var/lib/odoo/.grove-modules-rev`, host `/mnt/odoo-filestore/...`, survives a
  droplet replace). A restart on the **same** revision is a no-op — preserving
  the "no `--update` on every restart" design. Only a **revision advance** re-runs
  the upgrade:
  - **QA:** git-sync tracks `main`, so any merged module change triggers it on
    the next boot.
  - **Production:** git-sync is **SHA-pinned** (GOL-987), so the upgrade fires
    only on a deliberate pin bump — i.e. a real deploy, never a bare restart.
- **`--init` includes the modules** so a from-scratch environment self-installs
  them (full DDL) instead of the old undocumented manual `-i` step, and so
  `--update` never targets a not-installed module.
- **Fails loud.** Under `set -e` a failed upgrade aborts boot → `restart` loop,
  visible in `docker logs` — rather than serving a half-migrated DB. The marker
  is written **only after a successful upgrade**, so a failed migration retries
  on the next boot (no partial-state lock-in).

### Rollout note

`entrypoint.sh` is baked into the `grove-odoo` image (`docker-odoo.yml` rebuilds
+ publishes on merge to `main`). A running droplet picks up the new behavior on
its next image pull (`docker compose pull odoo && … up -d`) or on a droplet
replace. The compose `AUTO_UPGRADE_MODULES` var is read at container start, so it
takes effect on the next `up`/restart once the new image is present.

## Escape hatch — force a re-run without a redeploy

Use when you need to re-drive the upgrade on the **current** revision (e.g. after
a hand-edit, or to bootstrap the marker/tables on a droplet that predates this
fix). Run from an operator machine on the admin IP with the droplet SSH key:

```bash
# QA (default host)
scripts/module-upgrade.sh

# production
QA_HOST=root@<prod-odoo-host> scripts/module-upgrade.sh
```

It clears the revision marker and restarts odoo, so the entrypoint re-runs the
upgrade of `AUTO_UPGRADE_MODULES`. It reuses the tested code path (won't botch
`odoo.conf` generation the way an ad-hoc `docker run odoo -u …` would), and works
even if odoo is crash-looping (the marker is on the host bind-mount).

**A module NOT in `AUTO_UPGRADE_MODULES`:** add it to `AUTO_UPGRADE_MODULES` in
the droplet's `/etc/grove` compose env (comma-separated) and run the script; then
fold the addition back into the repo compose so it survives a replace.

## Promoting the prod modules pin -- Leg B (GOL-2346)

A Grove release promote is **two legs**, and only one of them is a workflow.

| Leg | What moves | How |
|---|---|---|
| **A -- storefronts** | `hub_image_tag` + `tenant_image_tag` | `.github/workflows/promote-storefronts.yml` (dispatch + `production` environment approval). It bumps the image tags **only** and hard-aborts if the bump ever touches `custom_modules_ref` (GOL-1708 finding 1). |
| **B -- modules** | `custom_modules_ref` | `scripts/prod-modules-promote.sh` (this section). Leg A cannot do it. |

**Why Leg B is not just another `terraform apply`.** `custom_modules_ref` flows
through `cloud-init-odoo.yaml.tpl` into the droplet's `user_data`, and
`digitalocean_droplet.odoo` carries `lifecycle { ignore_changes = [user_data,
monitoring] }`. A plain apply of a new pin is a **no-op**; forcing it through
means a droplet **replace** -- an outage plus the GOL-93 filestore gate. The
established safe path (precedents `34cc4548` / `22fbb71` / `0b36ecfa`) edits the
pin on the running box and lets git-sync plus the GOL-1009 entrypoint upgrade do
the rest.

### HARD PREREQUISITE -- soak the `-u` migrations on QA first

**Never run Leg B against prod with a migration that has not run on QA.** Any
target SHA whose `grove_headless` manifest version exceeds the installed one makes
the GOL-1009 entrypoint run `--update=grove_headless`, which executes every
`migrations/<version>/{pre,post}-migrate.py` between the two. On prod that is a
one-way door: the pin rolls back, the migration's data edits do not (see
"Rollback" in `RUNBOOK-release-train.md`).

```bash
# On QA, pinned to the SAME SHA you will hand Leg B. EXPECT_REF makes the script
# refuse a QA box that is not actually on the bundle SHA.
EXPECT_REF=<modules-sha> scripts/qa-module-upgrade.sh grove_headless
```

Then confirm each migration's effects **in the database**, not in the log:

- the recorded version advanced -- `SELECT latest_version FROM ir_module_module
  WHERE name = 'grove_headless';`
- every table / column / row the migrations write actually exists. A clean log
  line is a proxy, not the truth: a `-u` against a DB already past a migration's
  version legitimately **skips** it. The WV-tax bind is the standing example --
  it binds only on install or migration 1.47.0, so on a DB at >= 19.0.1.47.0 the
  log is clean and the only proof is the tax tables.

Record the soak result in the train manifest before Leg B. **If the soak did not
run, the module half of the train does not promote** -- it rides the next train.

### Run it

```bash
# 1. Pre-flight. Read-only: prints the current env pin, the live git-sync
#    checkout, the upgrade marker, the on-disk manifest versions, and the DB's
#    RECORDED grove_headless version (which decides whether the WV-tax
#    migration will actually run) -- then stops.
TARGET_REF=<40-hex reviewed grove-odoo-modules sha> \
  scripts/prod-modules-promote.sh

# 2. Execute (only the literal CONFIRM=PROMOTE mutates anything).
TARGET_REF=<same sha> CONFIRM=PROMOTE \
  scripts/prod-modules-promote.sh
```

Access is the same as every other L3 script: port 22 is firewalled to the admin
IP, so this runs from an operator machine, never from CI.

What the promote pass does, in order: backs up `/etc/grove/.env` to a timestamped
copy, upserts `CUSTOM_MODULES_REF` **exactly once** (a duplicate key is a silent
split-brain -- compose reads the last occurrence), force-recreates the
`custom-modules-sync` sidecar (`GITSYNC_REF` is resolved at container create, so
a bare `restart` would keep the old ref), waits for git-sync to actually land the
target commit, and only then restarts odoo so the entrypoint runs its blocking
`--init=base,<mods> --update=<mods>` pass. If git-sync never reaches the target,
**odoo is not restarted** and prod keeps serving the previous code.

### Activating a new container env var in the same touch (GOL-2507)

A secret `grove_headless` reads from `os.environ` needs **both** the droplet's
`/etc/grove/.env` line **and** the odoo service's compose `environment:`
passthrough -- the `/.env` mount only feeds `odoorc.sh`'s `odoo.conf`
substitution, so a var present only there **never reaches the process** (the
GOL-1935 footgun that left every Discord alert a silent no-op). Both points are
rendered from `user_data`, which prod's droplet carries in `ignore_changes` --
so merging the Terraform chain is a provable no-op and the **running** box only
picks the var up on a rebuild.

Rather than make that a second hand-edited prod touch on promote day, pass the
value in and the promote converges both points in place and **recreates** odoo:

```bash
PERENUAL_API_KEY="$(op read 'op://Goldberry Grove - Admin/perenual_api_key/credential')" \
  TARGET_REF=<same sha> CONFIRM=PROMOTE \
  scripts/prod-modules-promote.sh
```

- **Unset => skipped entirely.** The promote is byte-for-byte what it was.
- **The value never appears in argv** (either machine's process list) and is
  never echoed -- it travels to the droplet on stdin. A promote log is safe to
  paste into a ticket.
- **Validated locally first**, against the same contract
  `var.perenual_api_key`'s `validation` block enforces: cloud-init writes the
  value **unquoted** into a `set -euo pipefail` bash-sourced file, so a space or
  `$` would break the *next* boot, long after this run looked successful.
- **Recreate, not restart.** A container's env is fixed at create time; a plain
  `restart` would leave the whole converge invisible to the process. The
  recreate runs the same blocking entrypoint upgrade pass.
- **Verified against the process, not the files.** After the marker lands, the
  script reads `printenv PERENUAL_API_KEY` out of the container and **exits 10**
  if it is empty or different (values never echoed). Files converged + process
  not = the exact state that looks activated and enriches nothing.
- **Idempotent.** An empty `PERENUAL_API_KEY=` line (what cloud-init renders
  before the key is vaulted) is replaced, not duplicated; a second run is a
  `NO-OP`. And a droplet **already on the target SHA** does *not* take the
  `NO-OP` shortcut while a converge is still pending.
- **It is a bridge, not a snowflake.** The committed cloud-init + compose
  already render both lines (odoocker #724), so the next rebuild reproduces the
  box without this step; the on-box edit only stops the running droplet waiting
  for one.

After the promote, confirm Perenual actually drains rather than assuming it:
press **Fetch facts** on a plant product, check the `grove.enrich.job` row
reaches `done`, and check `ir.config_parameter
grove_headless.perenual_calls.<UTC-today>` increments. Prod's daily budget stays
**80** (`grove_headless.perenual_daily_budget`); QA holds the other 20 of the
shared vendor quota (one free-tier key, 100 calls/UTC day, counted per database).

### Flipping the Stripe Tax cutover flag on prod (GOL-2568 / GOL-2584)

`grove_headless` decides per order whether Stripe or Odoo computes sales tax by
reading `GROVE_STRIPE_TAX_{TENANT}` out of `os.environ`
(`controllers/main.py::_stripe_tax_enabled`). Unset, empty, or an unresolvable
tenant is **OFF**, and OFF is byte-identical to the pre-GOL-2568 checkout — so
OFF is also the rollback. Truthy values are `1` / `true` / `yes` / `on`.

**This is a money path: the flag changes what customers are charged.** Flip it
only as a separate, named, board-approved step, and only after the QA e2e gate
asserted amounts for a WV *and* a non-WV address (grove-odoo-modules
`docs/stripe-tax-cutover.md`, Gate 4).

**The train rule (ruled 2026-10-02, GOL-2584): ship inert, flip at the Wednesday
promote only if the gates passed.** The Terraform chain lands with
`grove_stripe_tax_tenants` empty, so prod keeps running the pre-GOL-2568 path no
matter what merged. Then, at promote (step 7 of the order of operations in
`RUNBOOK-release-train.md`):

- **Gates 3 + 4 green on QA** -- the gate runner is grove-odoo-modules #826, which
  reads the flag off observed **session behaviour** rather than a log line -> flip
  with Path A below, verify, and record it in the train issue.
- **Any gate unmet, inconclusive, or not run -> do not flip.** The flip rides the
  next train. This is a planned outcome, not an incident, and needs no escalation.
- **Rollback at any point = flag off**, which is byte-identical to the pre-flag
  path. In-flight Stripe sessions settle on the rules they were created with.

> ⚠️ **The deposit path bypasses Stripe Tax entirely.** `automatic_tax` is
> `tax_enabled and not is_deposit`, so a deposit order never exercises the Stripe
> computation. After the 2026-10-15 bareroot cutover the deposit path is the
> dominant one -- which means a green Gate 4 can be green on a road most orders
> have stopped taking. **Gate the flip on a non-deposit checkout assertion.**

All three keys are now rendered **unconditionally** by the committed Terraform
chain — `var.grove_stripe_tax_tenants` (a set of tenant slugs, empty by default)
→ `cloud-init-odoo.yaml.tpl` `/etc/grove/.env` → the odoo service's
`environment:` block in `compose/docker-compose.odoo.yml`. That matters because
of the GOL-1772/GOL-1786 footgun: a key that is **not listed in the deployed
compose** can never be injected into a running container no matter what
`/etc/grove/.env` says. With the keys always present, activation is a *value*
change, never a compose edit.

Because prod's droplet carries `user_data` in `ignore_changes`, merging the
Terraform change is a provable no-op on the running box, and a plain (or
`-target`ed) `terraform apply` will **not** activate the flag. Two real paths:

| Path | Cost | When |
|------|------|------|
| **A — env-file upsert** (GOL-1772 Option B, recommended) | seconds, no outage | promote day. On `grove-prod-odoo`: `sed -i '/^GROVE_STRIPE_TAX_NURSERY=/d' /etc/grove/.env && echo 'GROVE_STRIPE_TAX_NURSERY=1' >> /etc/grove/.env`, confirm `grep 'GROVE_STRIPE_TAX_NURSERY:' /etc/grove/docker-compose.yml` is present (add it to the odoo `environment:` block if the box predates this change), then `cd /etc/grove && docker compose --env-file /etc/grove/.env up -d --force-recreate --no-deps odoo`. **A container's env is fixed at create time — a plain `restart` does nothing.** |
| **B — droplet REPLACE** | ~10-20 min Odoo outage | only if a rebuild is already planned; re-triggers the GOL-93 filestore gate |

Verify against the **process**, not the files:

```bash
docker compose --env-file /etc/grove/.env exec -T odoo printenv GROVE_STRIPE_TAX_NURSERY
# -> 1        (files converged + process not = the state that looks live and charges Odoo tax)
```

Then place one real test order per address class and check
`sale.order.grove_stripe_tax_amount` / `grove_stripe_tax_jurisdictions` are
written back by the `checkout.session.completed` webhook.

**Rollback:** drop the line (or set it to `0`) and recreate the container the
same way. No code change, no data migration; in-flight Stripe sessions settle
normally and new sessions go back to the Odoo WV tax line.

**Follow-up (GOL-2584 child):** teach `scripts/prod-modules-promote.sh` to carry
`GROVE_STRIPE_TAX_NURSERY` the way it already carries `PERENUAL_API_KEY` above,
so the pin bump and the flag flip are one converge with process-level
verification instead of a hand-edited second touch.

### The money guard -- the real success condition

`grove_headless`'s `setup_wv_sales_tax` (`hooks.py`) wraps every company in
`try/except` and swallows failures at WARNING so tax setup can never abort an
upgrade. That is the right availability call, but it means **a partial bind exits
0 and looks like success** (GOL-2449). A company left on the old 7% / demo 15%
tax mis-charges every order it takes.

So "the upgrade finished" is **not** the success condition. This is:

```
grove_headless: WV 6% state sales tax bound for N of N companies
```

with both numbers equal. The script parses that line and **exits 9** on
`bound for only X of N`, echoing the per-company `WV tax setup FAILED` WARNINGs
that name the company.

#### …but the log line is a proxy, not the truth

`setup_wv_sales_tax` is reachable two ways: the `post_init_hook` (**fresh
install only**) and `migrations/19.0.1.47.0/post-migrate.py`. Odoo runs a
migration script only when the database's **recorded** version
(`ir_module_module.latest_version`) is *below* the script's version — so on a
database already recorded at `>= 19.0.1.47.0` the migration is **skipped
silently** and no bind line is ever logged.

That is not hypothetical. On **QA, 2026-09-23**, Josh's `-u grove_headless`
emitted no `Running migration [19.0.1.47.0]` and no bind line, because QA was
already recorded at `19.0.1.51.0`. The tax was in fact bound correctly for all
three companies — he had to prove it by reading the tables by hand in
`odoo shell`.

So the guard no longer trusts the log line alone:

- **Pre-flight** prints `grove_headless`'s recorded `installed_version` and says
  up front whether the WV-tax migration **will run** or **will be skipped**. A
  missing bind line is then an expectation, not a mid-promote surprise.
- **After the upgrade** the script always performs the authoritative read — the
  same one Josh ran by hand — straight out of the database:

  ```
  every res.company.account_sale_tax_id == "WV State Sales Tax 6%" @ 6.0
  every GROVE-SHIP product.taxes_id     == exactly that one tax
  ```

  (`GROVE-SHIP` is created lazily at first checkout; absent is reported `SKIP`,
  not a failure.)

| Exit | Meaning | What to do |
|---|---|---|
| `9` | A binding was **verified wrong** — either `bound for only X of N` in the log, or a `BAD` row in the DB read. Money defect. | Stop the promote. Prod is mis-charging those companies. Fix, then re-run. |
| `8` | The binding could **not be verified at all** — the `odoo shell` probe returned nothing. | Stop, but the fix is to *get a read*, not to roll back. Re-run the probe on the droplet (the script prints the exact command). |

A clean `3 of 3` in the log **cannot** launder a mis-bound database: the DB read
runs either way and wins. Do not take orders on a run that failed this check.

### Other fail-closed guards

- **Non-SHA `TARGET_REF`** is rejected locally, before the droplet is touched --
  same 40-hex contract `var.custom_modules_ref`'s own `validation` block enforces
  (GOL-892). Prod must never track a moving ref.
- **Deployed compose missing `AUTO_UPGRADE_MODULES`** aborts (exit 4). The
  deployed `/etc/grove/docker-compose.yml` is written from `user_data`, which is
  in `ignore_changes` -- so a box provisioned before the GOL-1009 wiring will not
  have it, and restarting would advance the code with **no migrations**. Hand-add
  the line (it is already in the source compose, so the edit is convergent) and
  re-run.
- **Idempotent.** Env pin + git-sync checkout + upgrade marker all already at the
  target reports `NO-OP` and changes nothing. Re-running a partially completed
  promote converges. A requested env converge (above) is part of that condition,
  so the shortcut can never skip an activation.
- **A broken compose edit is restored** (exit 10). The passthrough insert is
  anchored on `AUTO_UPGRADE_MODULES` and re-validated with `docker compose
  config`; on either failure the timestamped `docker-compose.yml.bak.*` is put
  back and nothing is recreated.

### Rollback

The script prints the exact three commands (restore the `.env` backup, recreate
the sidecar, clear the marker and restart). Odoo does not down-migrate, so treat
a modules rollback as an incident, not a routine undo -- the storefront leg is
the cheap one to revert (re-dispatch `promote-storefronts.yml` at the prior SHA).

### Then reconcile

Prod is now ahead of committed HCL. Close that with the reconcile workflow in the
next section -- **after** the live bump, never before (a catch-up PR must
converge onto what prod *is* serving; authoring it early recreates the #666/#667
competing-reconcile mess).

Regression tests for all of the above: `scripts/test_prod_modules_promote.py`
(simulated droplet, no network -- wired into the `Promotion script tests` CI job).

## Reconcile the committed default after a hand-edit (GOL-2281)

When you hand-edit `/etc/grove/.env`'s `CUSTOM_MODULES_REF` on the running prod
droplet (an incident fix — `ignore_changes` keeps `terraform apply` off the box),
the **live** pin moves but the **committed** default
(`var.custom_modules_ref` in `infra/terraform/environments/production/variables.tf`)
does not. The next droplet **rebuild** would then roll prod back onto the stale
committed SHA. Bringing the committed default back in line used to be a manual
PR (GOL-2232 #640, GOL-2273 #650, GOL-2280 #652 — three in one week).

End the ssh pin recipe with one line — it opens the reconcile PR as the bot:

```bash
# after: ssh prod droplet, edit CUSTOM_MODULES_REF=<sha> in /etc/grove/.env, restart odoo
gh workflow run reconcile-modules-pin.yml \
  --repo Goldberry-Playground/odoocker-goldberrygrove \
  -f modules_sha=<the 40-hex sha you pinned> \
  -f note='<optional context, e.g. GOL-2134 incident hand-edit>'
```

`reconcile-modules-pin.yml` edits `variables.tf` (value + a dated
`RECONCILE …` note appended to the description, drift-only, **no** roll-forward),
opens the PR, and does the wrong-block guard from GOL-1708. It **never** touches
prod. Merge the PR through the SHA-bound protected-paths-guard to un-drift `main`.
Idempotent: if the committed default already matches, it opens no PR. See the
workflow header for the GITHUB_TOKEN-checks caveat (GOL-2114).

## Verify

```bash
# On the droplet after a deploy / re-run:
docker compose --env-file /etc/grove/.env logs odoo | grep GOL-1009
#   -> "git-synced modules revision advanced ... running --init=base,grove_headless ..."
#   -> "module upgrade complete; recorded revision '<sha>' in /var/lib/odoo/.grove-modules-rev"

# Confirm the table exists (example: GOL-985 publish event):
docker compose --env-file /etc/grove/.env exec -T odoo \
  psql "$DB_HOST" -c '\d grove_publish_event'    # or via the app: click Publish, expect 200
```
