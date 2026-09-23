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

### Run it

```bash
# 1. Pre-flight. Read-only: prints the current env pin, the live git-sync
#    checkout, the upgrade marker and the on-disk manifest versions, then stops.
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

with both numbers equal. The script parses that line and **exits non-zero** on
`bound for only X of N` (echoing the per-company `WV tax setup FAILED` WARNINGs)
or on the line being absent entirely when `grove_headless` was in the upgrade
set. Do not take orders on a run that failed this check.

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
  promote converges.

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
