###############################################################################
# App Platform apps — PRODUCTION (ADR-007 Phase 6, Track 2 step 3 / GOL-116).
#
# Prod twin of qa-app-platform/apps.tf. Each of the 4 frontends (hub,
# goldberry, ggg, nursery) is its own digitalocean_app so a broken deploy in
# one tenant can't cascade to the others (App Platform concurrency is per-app).
#
# Image source: GHCR (grove-sites CI publishes on push to main). `deploy_on_push`
# redeploys on tag rotation, so a `latest` retag from grove-sites CI triggers a
# rollout — no rebuild inside App Platform. To freeze prod during an incident,
# pin var.hub_image_tag / var.tenant_image_tag to a SHA (deploy_on_push still
# fires only when THAT tag's digest moves, which a pinned SHA never does).
#
# Backend wiring vs QA:
#   - GROVE_ODOO_URL / ODOO_URL  → https://odoo.gatheringatthegrove.com  (the
#     Cloudflare-proxied Odoo droplet landed by PR #195 / odoo.tf, local.odoo_host).
#   - Ghost URLs  → the live blog.* hosts on the Track-1 blogs droplet
#     (blogs.tf) instead of QA's example.com stubs.
#   - Real per-tenant ODOO_API_KEY + shared GROVE_REVALIDATE_SECRET + real
#     Ghost content keys, all GENERAL scope (provider re-diffs SECRET envs every
#     plan — upstream #869/#514 — which would page the nightly drift alert).
#   - No NEXT_PUBLIC_QA_BANNER (this is prod, not a QA sandbox).
#   - instance_size_slug → pro tier (var.app_instance_size_slug, ADR-007 D6).
#
# ⚠️ NO domain{} blocks yet — see the "Apex cutover" section at the bottom.
# Without domain{}, each app serves ONLY on its *.ondigitalocean.app default
# ingress. That is the intended safe pre-cutover state: the four brand apexes
# keep serving Ghost (blogs droplet) until the coordinated launch-day flip.
#
# Apply is GATED on the GOL-105 QA L3 soak sign-off (~2026-07-21+) and the
# @CEO final go. This file scaffolds the spec; it does not authorize an apply.
###############################################################################

locals {
  # Frontend → its Ghost Content API origin. All four fetch content from the
  # blog.* host on the Track-1 blogs droplet, regardless of what the public
  # apex serves pre-cutover (blogs.tf serves the Ghost Content API on blog.*
  # from day one). Mirrors the blog_urls output.
  ghost_urls = { for k, z in local.tenants : k => "https://blog.${z}" }
}

# ── Hub ──────────────────────────────────────────────────────────────────────
#
# gatheringatthegrove.com's frontend. Reads from the Odoo droplet over its
# public (Cloudflare-proxied) URL — App Platform apps and DO droplets do not
# share a private network, so all Odoo/Ghost fetches go over TLS on the public
# internet. Cache TTLs live in grove-sites' KeyDB config, not here.
resource "digitalocean_app" "hub" {
  spec {
    name   = "grove-hub-prod"
    region = "nyc" # App Platform datacenter slug (NOT var.region's "nyc3" droplet slug).

    service {
      name               = "hub"
      instance_size_slug = var.app_instance_size_slug
      instance_count     = 1
      http_port          = 3000

      image {
        registry_type = "GHCR"
        registry      = "goldberry-playground"
        repository    = "grove-hub"
        tag           = var.hub_image_tag
        deploy_on_push {
          enabled = true
        }
      }

      health_check {
        http_path             = "/"
        initial_delay_seconds = 30
        period_seconds        = 30
        timeout_seconds       = 5
        success_threshold     = 1
        failure_threshold     = 3
      }

      # Hub's marketplace.ts reads process.env.GROVE_ODOO_URL.
      env {
        key   = "GROVE_ODOO_URL"
        value = "https://${local.odoo_host}"
        scope = "RUN_AND_BUILD_TIME"
      }

      # Hub's journal (apps/hub/app/journal/*.tsx) uses HUB_GHOST_URL +
      # HUB_GHOST_CONTENT_API_KEY. Points at the live blogs droplet.
      env {
        key   = "HUB_GHOST_URL"
        value = local.ghost_urls["hub"]
        scope = "RUN_AND_BUILD_TIME"
      }

      env {
        key   = "HUB_GHOST_CONTENT_API_KEY"
        value = var.ghost_content_keys["hub"]
        scope = "RUN_AND_BUILD_TIME"
      }

      # Signed webhook secret so grove-sites' /api/revalidate can be poked by
      # Odoo / Ghost on content change. GENERAL, not SECRET (see var docs).
      env {
        key   = "GROVE_REVALIDATE_SECRET"
        value = var.grove_revalidate_secret
        scope = "RUN_AND_BUILD_TIME"
      }

      env {
        key   = "NEXT_TELEMETRY_DISABLED"
        value = "1"
        scope = "RUN_AND_BUILD_TIME"
      }
    }

    # Alert path #2 per ADR-007 addendum (DO-native). Fires into DO's built-in
    # email channel; Discord routing via Keep is a separate wiring step.
    alert {
      rule = "DEPLOYMENT_FAILED"
    }
    alert {
      rule = "DOMAIN_FAILED"
    }

    # domain{} intentionally omitted — see "Apex cutover" at the bottom.
  }
}

# ── Tenant storefronts ────────────────────────────────────────────────────────
#
# Same shape as the hub, stamped out per tenant via for_each. Tenants read
# ODOO_URL (not the hub's GROVE_ODOO_URL) and GHOST_URL/GHOST_CONTENT_KEY, per
# tenant.secrets.ts. All three tenant images listen on port 3001 (ENV PORT=3001
# in each Dockerfile); only the hub differs (3000).
locals {
  tenant_apps = {
    goldberry = { image = "grove-goldberry" }
    ggg       = { image = "grove-ggg" }
    nursery   = { image = "grove-nursery" }
  }
}

resource "digitalocean_app" "tenant" {
  for_each = local.tenant_apps

  spec {
    name   = "grove-${each.key}-prod"
    region = "nyc"

    service {
      name               = each.key
      instance_size_slug = var.app_instance_size_slug
      instance_count     = 1
      http_port          = 3001

      image {
        registry_type = "GHCR"
        registry      = "goldberry-playground"
        repository    = each.value.image
        tag           = var.tenant_image_tag
        deploy_on_push {
          enabled = true
        }
      }

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

      # Real per-tenant bearer key (global-scope res.users.apikeys on the prod
      # Odoo). GENERAL, not SECRET (see var docs for the provider-drift reason).
      env {
        key   = "ODOO_API_KEY"
        value = var.odoo_api_keys[each.key]
        scope = "RUN_AND_BUILD_TIME"
      }

      # Shared revalidate secret so each tenant's /api/revalidate accepts the
      # signed webhook. Same value across all four apps.
      env {
        key   = "GROVE_REVALIDATE_SECRET"
        value = var.grove_revalidate_secret
        scope = "RUN_AND_BUILD_TIME"
      }

      env {
        key   = "GHOST_URL"
        value = local.ghost_urls[each.key]
        scope = "RUN_AND_BUILD_TIME"
      }

      env {
        key   = "GHOST_CONTENT_KEY"
        value = var.ghost_content_keys[each.key]
        scope = "RUN_AND_BUILD_TIME"
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

    # domain{} intentionally omitted — see "Apex cutover" below.
  }
}

###############################################################################
# ⚠️ Apex cutover — DEFERRED (one-way door, CEO-coordinated). GOL-116 decisions
# #1 and #2. Do NOT add domain{} blocks here until both are resolved:
#
#   #1 Custom-domain + Cloudflare-proxied apex pattern. QA L3 registers App
#      Platform custom domains inside a DO-DELEGATED zone; the prod brand apexes
#      live in Cloudflare and are proxied. App Platform wants to validate + issue
#      its own LE cert, but a CF-proxied CNAME in front needs Full(strict) SSL
#      and careful ownership validation. Resolve the exact pattern (CF CNAME →
#      app ingress, DNS-only during validation vs proxied, or CF Origin cert)
#      before scaffolding domain{}.
#
#   #2 Apex launch cutover. The four apexes currently serve Ghost (blogs
#      droplet, blogs.tf pre-launch URL policy). Flipping them to these App
#      Platform frontends is the coordinated launch-day cutover across all four
#      businesses — must be CEO-coordinated, not silently applied.
#
# When resolved, each app's spec gets a domain{} block along the lines of:
#
#   domain {
#     name = local.tenants[<key>]        # e.g. "gatheringatthegrove.com"
#     type = "PRIMARY"
#     # zone = ...   # only if App Platform manages the DNS record; with a
#                    # CF-proxied apex the CNAME/validation is managed in
#                    # Cloudflare (decision #1), so `zone` is likely omitted.
#   }
#
# and the matching cloudflare_record cutover (apex → app ingress) plus SSL mode
# is landed in the same coordinated apply. Until then these apps serve on their
# *.ondigitalocean.app default ingress only (see outputs), which is safe to
# apply post-soak without touching the live apexes.
###############################################################################
