# environments/production

ADR-007 Phase 6 production environment. Track 1 (blogs droplet) is live.
Track 2 (Managed PG + Odoo droplet, this scaffold) is authored but its **apply
is gated** on the QA L3 soak sign-off (~2026-07-21+) and the @CEO final go
(GOL-105). App Platform (the fourth piece) is a GOL-105 child issue.

## Apply (manual by decision)

1. `cp backend.hcl.example backend.hcl`
2. `.env.op` (committed in this dir) maps op:// refs -> TF_VAR_* + AWS_*. Required:
   - TF_VAR_do_token, TF_VAR_cloudflare_api_token (ACCOUNT-scoped token incl. "SSL and Certificates: Edit" - authorizes Origin CA cert issuance; the legacy Origin CA Key is deprecated)
   - TF_VAR_spaces_access_id, TF_VAR_spaces_secret_key
   - admin_ip_cidr + healthchecks_ping_url live in terraform.tfvars (gitignored)
   - AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (state backend)
3. `terraform init -backend-config=backend.hcl`
4. `op run --env-file=.env.op -- terraform plan`
5. `op run --env-file=.env.op -- terraform apply`

## What lives here (Track 1)

- Blogs droplet (4x Ghost 6 + MySQL 8 + Caddy) - see blogs.tf
- blog.{zone} DNS records in all four Cloudflare brand zones
- Cloudflare Origin CA certs (15y, per zone) for proxied TLS
- grove-blogs-backups Spaces bucket + lifecycle
- Apex A records for gatheringatthegrove.com + goldberrygrove.farm
  (imported from Cloudflare during migration; see apex-records.tf) (file arrives with the Task 6 migration)

## What lives here (Track 2 - GOL-105, apply gated)

- Managed Postgres cluster (basic tier, db-s-1vcpu-2gb, daily backups + 7d
  PITR, private VPC) + Odoo DB/user - see postgres.tf
- Odoo droplet (s-2vcpu-4gb) + Caddy (Origin CA cert files) + durable filestore
  block volume (GOL-93) + Managed PG trusted-sources firewall - see odoo.tf
- odoo.gatheringatthegrove.com Cloudflare-proxied A record (the record GOL-93's
  /web/image edge-cache rule was waiting on)
- Reuses Track 1's SSH keys + the hub-zone Origin CA cert (its
  `*.gatheringatthegrove.com` SAN covers `odoo.`)

**Soak sign-off (GOL-105) must be green before `terraform apply`:** Managed PG
perf + Odoo pool acceptable; App Platform TLS auto-renew clean; GHCR autodeploys
reliable; durable filestore + droplet-replace test validated (GOL-93); three
alert paths green; no unresolved incidents across the window. Then @-mention the
CEO here for the final go.

Still pending (GOL-105 child): App Platform specs (hub + 3 tenants, pro tier) +
the brand-apex launch cutover.

## History

The pre-Level-3 monolith production config that previously lived here
(never applied; "DO NOT DEPLOY YET") was replaced 2026-07 by this
Phase 6 shape per the Grove Production Launch spec.
