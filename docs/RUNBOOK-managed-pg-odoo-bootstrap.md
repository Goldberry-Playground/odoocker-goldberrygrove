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
`production/postgres.tf` creates an empty `postgres` maintenance DB (the probe
only connects; it never writes there — Odoo inits the real `odoo` DB). No
action needed on a fresh apply; the resource creates it.

## Gotcha 2 — Odoo's DB user cannot CREATE in schema `public` (MANUAL)

`digitalocean_database_db.odoo` is owned by the cluster's `doadmin`, not by the
scoped `odoo` user. On PostgreSQL 15+ (prod is pg17) only the database owner has
CREATE on schema `public`, so Odoo's first `--init=base` dies with
`InsufficientPrivilege: permission denied for schema public` on the very first
`CREATE TABLE`.

This step is deliberately **not** codified on the droplet: the droplet holds
only the least-privilege `odoo` user creds (the cluster admin password never
lands on a droplet — see `postgres.tf`), and `odoo` cannot grant itself. Run it
once, out-of-band, as `doadmin`, after the cluster + `odoo` db exist and before
(or during) first Odoo boot:

```sh
# Get the private-network doadmin URI (run from an allowlisted host — the Odoo
# droplet is in the cluster's trusted sources; your workstation is not unless
# you add its /32 to the odoo.tf trusted_sources / firewall temporarily).
CID=$(doctl databases list --format ID,Name --no-header | awk '$2=="grove-prod-pg"{print $1}')
URI=$(doctl databases connection "$CID" --private --format URI --no-header)
ODOO_URI=$(echo "$URI" | sed 's#/defaultdb?#/odoo?#')

psql "$ODOO_URI" -v ON_ERROR_STOP=1 <<'SQL'
ALTER DATABASE odoo OWNER TO odoo;   -- makes pg_database_owner resolve to odoo → CREATE on public
GRANT ALL ON SCHEMA public TO odoo;  -- explicit, belt-and-suspenders
SQL

# Verify:
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

## Follow-up (codify Gotcha 2 / kill the drift)

- Backfill `digitalocean_database_db.postgres` **and** the ownership grant into
  `qa-app-platform/` so QA is reproducible from code (it currently relies on the
  same undocumented hand-fixes).
- Evaluate codifying Gotcha 2 via the `cyrilgdn/postgresql` provider
  (authenticated as `doadmin` from `digitalocean_database_cluster.pg.password`).
  Blocker: the cluster's trusted-sources firewall allowlists only the droplet +
  operator CIDR, so the TF runner can't connect at plan time without a firewall
  carve-out — resolve that before adopting the provider.
- Root-cause option: ship a grove-odoo `wait-for-psql.py` that probes
  `defaultdb` instead of `postgres`, removing the need for Gotcha 1's stray db.
