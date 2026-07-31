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
