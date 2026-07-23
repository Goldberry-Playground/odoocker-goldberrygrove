# Runbook — Odoo on DO Managed Postgres: first-boot DB bootstrap

Two DO-Managed-Postgres-specific gotchas block Odoo from bootstrapping on a
**fresh** managed cluster. Both surfaced on the prod Track-2 keystone bring-up
(GOL-737, 2026-07-23) where Odoo crash-looped 21× and the edge served `502`.
QA L3 dodged both only because they had been hand-fixed there (click-op drift
never captured in `qa-app-platform/`), so the prod scaffold — copied from that
TF — inherited the omissions. Owner: DevOps - Terra. Parent: GOL-104 / GOL-737.

**When this applies:** any *from-scratch* `terraform apply` of a `production/`
or `qa-app-platform/`-shaped env against a **new** managed PG cluster. It does
**not** apply to a droplet *replace* — the cluster (and both fixes below)
persists across droplet recreates, so a replaced droplet boots clean.

---

## Gotcha 1 — the `postgres` maintenance database (CODIFIED)

The grove-odoo image's readiness probe (base `odoo:19`
`/usr/local/bin/wait-for-psql.py`, invoked from `odoo/entrypoint.sh`) connects
with a **hardcoded** `dbname='postgres'` before Odoo starts. DO Managed PG
ships `defaultdb` and has **no** `postgres` database, so the probe fails forever
(`FATAL: database "postgres" does not exist`) and Odoo crash-loops.

**Fix (now in code):** `digitalocean_database_db.postgres` in
**both** `production/postgres.tf` **and** `qa-app-platform/main.tf` (backfilled
GOL-750) creates an empty `postgres` maintenance DB (the probe only connects;
it never writes there — Odoo inits the real `odoo` DB). On a fresh apply the
resource creates it; no action needed.

**QA one-time import (GOL-750):** QA's live `postgres` DB was hand-created
before the resource existed, so import it once before the next QA apply or the
apply 409s "database already exists":

```sh
CID=$(doctl databases list --format ID,Name --no-header | awk '$2=="grove-qa-l3-pg"{print $1}')
terraform import digitalocean_database_db.postgres "$CID/postgres"   # run in qa-app-platform/
```

## Gotcha 2 — Odoo's DB user cannot CREATE in schema `public` (CODIFIED, GOL-750)

`digitalocean_database_db.odoo` is owned by the cluster's `doadmin`, not by the
scoped `odoo` user. On PostgreSQL 15+ (prod is pg17) only the database owner has
CREATE on schema `public`, so Odoo's first `--init=base` dies with
`InsufficientPrivilege: permission denied for schema public` on the very first
`CREATE TABLE`.

**Fix (now in code):** `terraform_data.pg_schema_owner_grant` (in
`production/postgres.tf` and `qa-app-platform/main.tf`) runs the grant as a
`local-exec` provisioner during `terraform apply` — **not** a `cyrilgdn/
postgresql` provider resource, which would refresh (and so connect) at *plan*
time and break the CI drift plan from a non-allowlisted runner (see the
resource comment for the full rationale). It runs the same SQL a human used to
run by hand:

```sql
ALTER DATABASE odoo OWNER TO odoo;   -- makes pg_database_owner resolve to odoo → CREATE on public
GRANT ALL ON SCHEMA public TO odoo;  -- explicit, belt-and-suspenders
```

**Apply-host prerequisites** (Grove applies are manual + gated, run from the
operator machine whose /32 is in `var.admin_ip_cidr`):

- `psql` (`postgresql-client`) on `PATH` — the provisioner fails loud with a
  clear message if it is missing, *before* attempting the grant.
- Outbound TCP 25060 to the cluster's **public** host (the provisioner connects
  over the public endpoint from the allowlisted operator IP; the firewall's
  `ip_addr` rule already permits this — no temporary carve-out needed).

The provisioner runs only on create/replace and never on a plan/refresh, so CI
plans stay green. The SQL is idempotent, so re-running (e.g. after a `terraform
taint`) is safe.

Verify after apply:

```sh
psql "$ODOO_URI" -tAc "SELECT has_schema_privilege('odoo','public','CREATE');"  # → t
```

Then restart the Odoo container (`docker restart grove-odoo-1`) to trigger a
clean `--init=base`; watch `docker logs -f` for `Modules loaded.` /
`Registry loaded`.

---

## Verification (keystone acceptance)

```sh
curl -s https://odoo.gatheringatthegrove.com/web/health          # → {"status": "pass"} (200)
# filestore writable by the container odoo user (uid=100 gid=101, NOT 101:101):
docker exec grove-odoo-1 sh -c 'id; touch /var/lib/odoo/filestore/.probe && echo WRITABLE'
findmnt -no SOURCE,TARGET /var/lib/odoo   # → LABEL=filestore block volume (durable, not root disk)
```

## Status (GOL-750)

- ✅ **QA drift backfilled** — `digitalocean_database_db.postgres` is now in
  `qa-app-platform/main.tf` (+ one-time import above); `ignore_changes =
  [settings]` added to the QA `odoo` user to match prod.
- ✅ **Gotcha 2 codified** — `terraform_data.pg_schema_owner_grant` in both
  envs (see Gotcha 2). The `cyrilgdn/postgresql` provider was **evaluated and
  rejected**: it connects at plan time, which would break the CI drift plan
  (`.github/workflows/terraform-drift.yml`) from a non-allowlisted runner and
  demand a prod firewall carve-out for dynamic runner IPs. `local-exec` (create
  only, never on plan) resolves the blocker without any firewall change.
- ⏳ **Root-cause option (stretch, Engineering-Alice)** — ship a grove-odoo
  `wait-for-psql.py` that probes `defaultdb` instead of `postgres`, removing the
  need for Gotcha 1's stray maintenance DB entirely. It is an image change in
  `grove-odoo-modules`/the grove-odoo image, tracked as a GOL-750 child issue.
  It does **not** eliminate Gotcha 2 (schema ownership is independent of the
  probe).
