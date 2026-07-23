###############################################################################
# Track 2, step 1 - Managed Postgres (ADR-007 Phase 6, GOL-105).
#
# Copied + sized up from the validated QA L3 env
# (environments/qa-app-platform/main.tf "Managed Postgres" block). The core
# architectural shift of Level 3: Postgres leaves the Odoo droplet entirely.
# Odoo connects over the private VPC network (odoo.tf wires the private_host
# into the droplet's compose env).
#
# QA -> prod size-up (ADR-007 D6 budget, ~$30/mo line item):
#   - Size:  db-s-1vcpu-1gb (dev, ~$15/mo)  ->  db-s-1vcpu-2gb (basic, ~$30/mo)
#   - Tier:  dev (1-day backups, no PITR)   ->  basic (daily backups + 7-day
#            point-in-time recovery, both automatic on the basic tier - no TF
#            arg needed; this is the value prop that justifies the prod spend
#            per ADR-007 D3/D6).
#   - HA:    node_count stays 1. A standby (+$30/mo) is deferred per ADR-007 D6
#            "until traffic warrants it" - flip var.pg_node_count to add one.
#
# The trusted-sources firewall for this cluster lives in odoo.tf (Track 2
# step 2): it allowlists the Odoo droplet's ID + the operator CIDR, and the
# droplet resource it references only exists once step 2 lands. Until then the
# cluster is reachable only on its DO-assigned private/public hostnames with
# DO's default (no public trusted sources) - and prod apply is gated on the
# soak sign-off regardless (GOL-105).
###############################################################################

resource "digitalocean_database_cluster" "pg" {
  name       = "grove-prod-pg"
  engine     = "pg"
  version    = var.pg_version
  size       = var.pg_size
  region     = var.region
  node_count = var.pg_node_count
  tags       = concat(local.tags, ["role-postgres"])

  # The production ERP database. Deleting the cluster also deletes its
  # automated backups and PITR history with it, so `terraform destroy`
  # must refuse until this guard is deliberately removed in a reviewed
  # PR. (#237)
  lifecycle {
    prevent_destroy = true
  }
}

# Odoo-side DB + least-privilege DB user. Managed via TF (not the cluster's
# default `doadmin`) so Odoo gets scoped creds and the cluster's primary admin
# password never lands on the droplet. Same pattern as QA L3.

resource "digitalocean_database_db" "odoo" {
  cluster_id = digitalocean_database_cluster.pg.id
  name       = "odoo"
}

# Maintenance/bootstrap database named `postgres`.
#
# The grove-odoo image's readiness probe (base odoo:19 `/usr/local/bin/
# wait-for-psql.py`, invoked from odoo/entrypoint.sh) connects with a
# HARDCODED `dbname='postgres'` before Odoo starts -- it just needs *any*
# reachable DB to confirm the server is up; it never writes here (Odoo inits
# the real `odoo` DB above). DO Managed Postgres, however, ships `defaultdb`
# and has NO `postgres` database, so on a fresh managed cluster the probe
# fails forever ("FATAL: database \"postgres\" does not exist") and Odoo
# crash-loops (observed on the prod keystone bring-up: 21 restarts, 502 at the
# edge). QA L3 only worked because a `postgres` db was hand-created there
# (click-op drift never captured in qa-app-platform/main.tf), so the prod
# scaffold -- copied from that TF -- inherited the omission.
#
# Codifying it here makes prod come up from code alone (GOL-737). Follow-up:
# backfill the same resource into the QA env to kill the drift, and consider a
# root-cause image fix (probe `defaultdb` instead of `postgres`) so no stray
# maintenance DB is needed.
resource "digitalocean_database_db" "postgres" {
  cluster_id = digitalocean_database_cluster.pg.id
  name       = "postgres"
}

resource "digitalocean_database_user" "odoo" {
  cluster_id = digitalocean_database_cluster.pg.id
  name       = "odoo"
  # SCRAM-SHA-256 is enforced server-side regardless of auth plugin.

  # DO returns an empty `settings {}` block on a user created without one, and
  # the provider then plans to remove it - a PUT the DO API rejects with
  # "missing required fields: user_settings" (400). The block is cosmetic (no
  # opensearch ACLs / mongo roles on a PG user), so ignore it to keep plans
  # clean. Applied during the keystone bring-up (GOL-737).
  lifecycle {
    ignore_changes = [settings]
  }
}
