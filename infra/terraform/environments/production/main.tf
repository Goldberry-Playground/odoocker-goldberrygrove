###############################################################################
# Grove PRODUCTION environment (ADR-007 Phase 6 shape).
#
# Build-out order (Grove Production Launch spec, 2026-07-04):
#   1. blogs.tf      - Ghost blogs droplet + blog.* DNS + backups   (Track 1)
#   2. postgres.tf   - Managed Postgres (basic tier, backups/PITR)  (Track 2, GOL-105)
#   3. odoo.tf       - Odoo droplet + Caddy + durable filestore vol (Track 2, GOL-105)
#   4. App Platform  - hub + 3 tenant frontends, pro tier           (Track 2, GOL-105 child)
#
# Track 2 apply is GATED on the QA L3 soak sign-off (~2026-07-21+) and the
# @CEO final go (GOL-105). App Platform (step 4) is carved into a child issue
# because its custom domains are the four live brand APEXES - the launch
# cutover is a one-way door needing CEO coordination.
#
# Track 1 replaces the two hand-managed Ghost snowflake droplets
# (gatheratthegrove-blog-nyc, ghostgoldberrygrove-nyc1). Until the launch
# cutover, the brand apexes continue to serve Ghost - from this env's
# blogs droplet once Tasks 6/7 repoint them.
#
# Apply is MANUAL by decision (no CI apply for production):
#   cd infra/terraform/environments/production
#   terraform init -backend-config=backend.hcl
#   op run --env-file=.env.op -- terraform apply
###############################################################################

provider "digitalocean" {
  token             = var.do_token
  spaces_access_id  = var.spaces_access_id
  spaces_secret_key = var.spaces_secret_key
}

provider "cloudflare" {
  # Origin CA certs (blogs.tf, cloudflare_origin_ca_certificate) authenticate
  # with THIS scoped API token — no service key. Provider supports token auth
  # for Origin CA since v3.32.0 (prod is pinned ~> 4.40). The token needs
  # Zone -> SSL and Certificates -> Edit on all four zones; verified live
  # 2026-09-29 (token id a9caef2f: POST/GET /certificates authorize on all four
  # brand zones — GOL-2317).
  # The legacy Origin CA Key (X-Auth-User-Service-Key) Cloudflare deprecated
  # 2026-09-30 is NOT used here and was never vaulted, so its retirement is a
  # no-op for this env: a droplet rebuild after the deadline mints certs
  # token-only, no fallback needed.
  # developers.cloudflare.com/changelog/post/2026-03-19-service-key-authentication-deprecated/
  # This env feeds the token at 1P ref `op://Grove Prod/Cloudflare API Token/credential`
  # (repointed GOL-1770; the old `account_cloudflare_api_token` field expired 2026-08-19).
  api_token = var.cloudflare_api_token
}

locals {
  tags = [
    "env-production",
    "project-grove",
  ]

  # Tenant -> brand zone. Drives blog.* records, origin certs, Ghost URLs.
  tenants = {
    hub       = "gatheringatthegrove.com"
    goldberry = "goldberrygrove.farm"
    ggg       = "woodworkingeorge.com"
    nursery   = "atthegrovenursery.com"
  }
}

data "cloudflare_zone" "brand" {
  for_each = local.tenants
  name     = each.value
}
