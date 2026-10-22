###############################################################################
# Grove Consult host — PRODUCTION (consult phase 2 E, GOL-3577).
#
# Spec: grove-odoo-modules docs/superpowers/specs/2026-10-22-consult-dashboard-
# design.md §D/§E. The consult dashboard (grove-sites apps/consult, Iris,
# GOL-3576) is an INTERNAL staff tool served on consult.gatheringatthegrove.com.
# Three things live here, all of them separate from the four revenue apps:
#
#   1. digitalocean_app.consult      — its own App Platform app (never a
#                                      component inside grove-nursery-prod), so
#                                      a broken consult deploy, a bad spec, or
#                                      the kill switch below cannot touch the
#                                      nursery storefront.
#   2. cloudflare_record.consult     — proxied CNAME consult.<hub apex> → the
#                                      app's default ingress, same CF-proxied-
#                                      CNAME → *.ondigitalocean.app pattern as
#                                      the apex cutover (apex-cutover.tf).
#   3. digitalocean_spaces_bucket_object.consult_bundles_readme
#                                    — settles the Spaces prefix `consult/
#                                      <opportunity>/` for the phase-4 export
#                                      bundles (plan PDF, GIS, lot list).
#
# The Cloudflare ACCESS application + staff-email policy for this host are NOT
# here: this env's Cloudflare token is zone-scoped (DNS/SSL/Settings/Redirects,
# see var.cloudflare_api_token) and has no Account→Access permission. Access
# lives where Paperclip's does, with the token that already has Access:Edit --
# AgenticOS infra/terraform/cloudflare-access-consult.tf (same Google IdP, same
# policy shape as `paperclip`). "No new credentials" (spec §E) is why the split
# exists. The two halves meet through var.consult_access_aud (below).
#
# ── Gate: var.consult_enabled (default false) ───────────────────────────────
# The image ghcr.io/goldberry-playground/grove-consult:<sha> does not exist
# until grove-sites' Docker — Frontends matrix gains the `consult` leg (rides
# Iris's apps/consult PR). An App Platform app whose image cannot be pulled
# fails its first deploy and sits in ERROR -- harmless to the other apps but
# noisy -- so the app AND its DNS record are `count`-gated off until Train #6.
# Flip to true in the same PR that bumps the pin to a SHA whose
# `grove-consult` image is on GHCR. Flipping back to false is the codified
# KILL SWITCH: the app and the CNAME are destroyed, the nursery is untouched
# (see docs/RUNBOOK-consult-host.md).
#
# ── Deploy owner ─────────────────────────────────────────────────────────────
# Prod applies on this state are split across three owners (never a bare
# `terraform apply` -- prod-plan-guard.yml explains why):
#   (a) promote-storefronts.yml  — App Platform frontends, targeted apply +
#                                  per-app `doctl create-deployment`.
#   (b) prod-modules-promote.sh / reconcile-modules-pin.yml — the Odoo
#                                  droplet's custom_modules_ref.
#   (c) Josh, manual targeted apply — everything else (DNS, firewalls, TLS,
#                                  buckets).
# digitalocean_app.consult is owner (a): it pins var.tenant_image_tag (the
# shared grove-sites SHA, same reason the three tenants share one tag) and
# promote-storefronts.yml targets it and rolls it after the four storefronts
# whenever the app exists. The CNAME + the Spaces README are owner (c): one
# manual `-target` apply at first enable, then inert.
###############################################################################

locals {
  consult_host = "consult.${local.tenants["hub"]}" # consult.gatheringatthegrove.com

  # Spaces prefix for the phase-4 export bundles. `<opportunity>` is the
  # crm.lead id plus the slugified lead name (e.g. 142-smith-hollow) -- the same
  # key AgriforestryOS scripts/new_client_project.py uses for the per-client
  # QGIS project, so one string finds a client in Odoo, on Spaces and in QGIS.
  consult_bundles_prefix = "consult/"
}

# ── 1. App Platform app ──────────────────────────────────────────────────────
#
# Same image family + deploy mechanism as the nursery app: GHCR image published
# by grove-sites' Docker — Frontends workflow at the shared SHA, SHA-pinned,
# deploy_on_push off (GOL-1304 Option-A; moot for GHCR anyway, GOL-1607), rolled
# by `doctl apps create-deployment`. Differences from the nursery, on purpose:
#   - BASIC tier, 1 instance (var.consult_instance_size_slug): a handful of
#     staff sessions, no checkout, no market-season load. ~$5/mo vs ~$12.
#   - http_port 3001: apps/consult uses the nursery toolchain (spec §D), whose
#     Dockerfile sets PORT=3001. MUST match apps/consult/Dockerfile -- if Iris
#     ships a different port, change it here in the same train.
#   - No Ghost / newsletter / livechat env. The consult BFF talks to Odoo only.
resource "digitalocean_app" "consult" {
  count = var.consult_enabled ? 1 : 0

  spec {
    name   = "grove-consult-prod"
    region = "nyc"

    service {
      name               = "consult"
      instance_size_slug = var.consult_instance_size_slug
      instance_count     = 1
      http_port          = 3001

      image {
        registry_type = "GHCR"
        registry      = "goldberry-playground"
        repository    = "grove-consult"
        tag           = var.tenant_image_tag
        deploy_on_push {
          enabled = false
        }
      }

      # The app sits behind Cloudflare Access; "/" renders the pipeline shell
      # (or its login-less placeholder) -- no auth challenge on the origin
      # itself, so the platform health check sees a 200.
      health_check {
        http_path             = "/"
        initial_delay_seconds = 30
        period_seconds        = 30
        timeout_seconds       = 5
        success_threshold     = 1
        failure_threshold     = 3
      }

      env {
        key   = "ODOO_URL"
        value = "https://${local.odoo_host}"
        scope = "RUN_AND_BUILD_TIME"
      }

      # Spec §C.7: the consult staff routes are "authenticated with the
      # existing headless API key the storefront BFF holds" -- no new
      # credential. The nursery's global-scope key is that key. If a dedicated
      # `consult` key is ever minted into ODOO_API_KEYS_TF_JSON it takes over
      # automatically; until then the lookup falls through to nursery.
      env {
        key   = "ODOO_API_KEY"
        value = lookup(var.odoo_api_keys, "consult", var.odoo_api_keys["nursery"])
        scope = "RUN_AND_BUILD_TIME"
      }

      # ⚠️ Cloudflare Access only guards the vanity host. The app's default
      # *.ondigitalocean.app ingress is public and App Platform has no origin
      # IP allowlist, so the BFF MUST verify the `Cf-Access-Jwt-Assertion`
      # header (issuer https://<team>.cloudflareaccess.com, audience = the
      # Access app's AUD tag) on every request and 403 otherwise. These two
      # envs are what that check needs. The AUD comes from the AgenticOS
      # Terraform output `consult_access_aud`; while it is empty the env is
      # omitted and the app must fail CLOSED (treat missing config as deny).
      env {
        key   = "CF_ACCESS_TEAM_DOMAIN"
        value = var.consult_access_team_domain
        scope = "RUN_TIME"
      }

      dynamic "env" {
        for_each = var.consult_access_aud != "" ? [1] : []
        content {
          key   = "CF_ACCESS_AUD"
          value = var.consult_access_aud
          scope = "RUN_TIME"
        }
      }

      env {
        key   = "NEXT_TELEMETRY_DISABLED"
        value = "1"
        scope = "RUN_AND_BUILD_TIME"
      }
    }

    alert {
      rule = "DEPLOYMENT_FAILED"
    }
    alert {
      rule = "DOMAIN_FAILED"
    }

    # Registers the Host so DO's edge routes it (same reason as the apex
    # cutover domain{} blocks: an unregistered Host 403s fail-closed). PRIMARY,
    # zone omitted -- DNS is the Cloudflare record below, not DO-managed.
    domain {
      name = local.consult_host
      type = "PRIMARY"
    }
  }
}

# ── 2. DNS: consult.<hub apex> → app ingress, proxied ────────────────────────
#
# Proxied so Cloudflare Access can sit in front (Access only applies to
# proxied hostnames). CF connects to the origin with SNI = the
# ondigitalocean.app host (valid wildcard cert) and forwards Host =
# consult.gatheringatthegrove.com, which the domain{} block above registers.
# Verified pattern: apex-cutover.tf ("CF PROXIED CNAME → *.ondigitalocean.app
# resolves and serves 200 with NO Host-header rewrite").
resource "cloudflare_record" "consult" {
  count = var.consult_enabled ? 1 : 0

  zone_id = data.cloudflare_zone.brand["hub"].id
  name    = "consult"
  type    = "CNAME"
  value   = trimsuffix(trimprefix(digitalocean_app.consult[0].default_ingress, "https://"), "/")
  proxied = true
  ttl     = 1 # auto (required when proxied)
  comment = "GOL-3577 consult dashboard -> grove-consult-prod App Platform ingress; Cloudflare Access (AgenticOS TF) gates it"
}

# ── 3. Spaces prefix for the phase-4 export bundles ──────────────────────────
#
# Bucket: grove-odoo-backups (odoo.tf). Why that bucket and not a new one:
#   - private ACL + prevent_destroy already in place -- the bundle IS the
#     client's archive-of-record after handover (spec: "the export bundle on
#     Spaces is the backup. Nothing is deleted."), so it belongs with the other
#     things we must never lose, not in the public-read assets bucket.
#   - no new credential: the Odoo droplet already holds the bucket-scoped
#     readwrite key `grove-odoo-backup` (digitalocean_spaces_key.odoo_backup),
#     and the phase-4 export runs from grove-odoo-modules on that droplet.
#   - RETENTION: indefinite. The bucket's only lifecycle_rule expires
#     filestore/archive/ after 35 days; nothing under consult/ matches it and
#     no rule is added here on purpose. Deleting a bundle is a human decision
#     made in the console, never a timer.
#
# Layout (one folder per opportunity, one sub-folder per bundle revision):
#   consult/<opportunity>/<YYYY-MM-DD>-v<N>/{plan.pdf,site-brief.pdf,
#     plantings.geojson,plantings.kml,planting-points.csv,<opportunity>.qgz,
#     calendar.ics,pruning-guides.pdf,lots.csv,manifest.json}
#
# S3-style stores have no real folders; this README object is what makes the
# prefix exist, documents it next to the data, and gives `terraform plan` a
# resource to diff. Idempotent: same key + same content = no-op.
resource "digitalocean_spaces_bucket_object" "consult_bundles_readme" {
  region       = digitalocean_spaces_bucket.odoo_backups.region
  bucket       = digitalocean_spaces_bucket.odoo_backups.name
  key          = "${local.consult_bundles_prefix}README.md"
  acl          = "private"
  content_type = "text/markdown"
  content      = <<-MD
    # consult/ — Grove Consult export bundles (GOL-3577, consult phase 2 E)

    One folder per client opportunity, one sub-folder per bundle revision:

        consult/<opportunity>/<YYYY-MM-DD>-v<N>/

    `<opportunity>` = `<crm.lead id>-<slugified lead name>` (e.g. `142-smith-hollow`),
    the same key as the client's QGIS project (`<opportunity>.qgz`, made by
    AgriforestryOS scripts/new_client_project.py).

    Written by the phase-4 export (grove-odoo-modules) with the Odoo droplet's
    bucket-scoped key `grove-odoo-backup`. Retention is INDEFINITE: no lifecycle
    rule covers this prefix (the bucket's only rule expires filestore/archive/).
    The bundle is the client's archive-of-record after handover; delete only by
    hand, never by timer. Managed by odoocker-goldberrygrove
    infra/terraform/environments/production/consult.tf.
  MD
}
