###############################################################################
# App Platform apps — PRODUCTION (ADR-007 Phase 6, Track 2 step 3 / GOL-116).
#
# Prod twin of qa-app-platform/apps.tf. Each of the 4 frontends (hub,
# goldberry, ggg, nursery) is its own digitalocean_app so a broken deploy in
# one tenant can't cascade to the others (App Platform concurrency is per-app).
#
# Image source: GHCR (grove-sites CI publishes on push to main). GOL-1304
# Option-A ruling (launch-gate Aug 20): prod runs PINNED SHA releases, not a
# continuously-tracked `latest`, so deploy_on_push is DISABLED here and
# var.hub_image_tag / var.tenant_image_tag hold a 40-char grove-sites commit SHA
# (default b84d7678, the currently-serving build — GOL-1650). Two reasons the
# pin is the honest model: (1) deploy_on_push is a DOCR-only trigger and never
# fires for GHCR-sourced apps anyway (GOL-1607), so leaving it "enabled" against
# a `latest` tag was both un-gated AND unobservable; (2) a prod rollout must be a
# reviewed infra PR (bump the SHA here) whose deploy step is an explicit
# `doctl apps create-deployment` via the grove-sites do-app-redeploy primitive
# (PR #547) — not an implicit CI side effect. The grove-sites drift alarm (#547)
# pages when a prod app's serving digest != the digest this pinned SHA resolves
# to on GHCR.
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
        # GOL-1304 Option-A: prod ships pinned SHA releases via reviewed infra
        # PRs, so no auto-redeploy on tag rotation. (Moot for GHCR anyway --
        # deploy_on_push is DOCR-only and never fires here, GOL-1607.)
        deploy_on_push {
          enabled = false
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

      # Hub's /api/assets/optimize (apps/hub/lib/assets/service.ts) plus the
      # ADR-009 social/ re-host seam: Spaces upload credentials + the shared
      # bearer the discord-bridge presents on its forward call. RUN_TIME only --
      # the route reads these at request time, never during the Next build, so
      # they stay out of the build environment. GENERAL (not SECRET) for the
      # same provider re-diff reason documented on odoo_api_keys.
      env {
        key   = "GROVE_ASSETS_KEY"
        value = var.grove_assets_key
        scope = "RUN_TIME"
      }

      env {
        key   = "GROVE_ASSETS_SECRET"
        value = var.grove_assets_secret
        scope = "RUN_TIME"
      }

      env {
        key   = "GROVE_ASSETS_OPTIMIZE_TOKEN"
        value = var.grove_assets_optimize_token
        scope = "RUN_TIME"
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

    # Apex cutover (GOL-1390 / GOL-1279 finding #1) — the hub is a SEPARATE
    # resource from digitalocean_app.tenant, so the tenant for_each's domain
    # block never covers it. Without this block, adding "hub" to
    # var.apex_cutover_live_keys is a SILENT NO-OP → gatheringatthegrove.com
    # would 403 fail-closed after its apex DNS flip (DO won't route a Host it
    # hasn't registered). Same per-apex gate + PRIMARY/zone-omitted pattern as
    # the tenant block above; local.tenants["hub"] = gatheringatthegrove.com.
    dynamic "domain" {
      for_each = contains(var.apex_cutover_live_keys, "hub") ? [1] : []
      content {
        name = local.tenants["hub"]
        type = "PRIMARY"
      }
    }
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
        # GOL-1304 Option-A: pinned SHA releases via reviewed infra PRs, no
        # auto-redeploy on tag rotation (deploy_on_push is DOCR-only anyway,
        # GOL-1607).
        deploy_on_push {
          enabled = false
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

    # Apex cutover (GOL-1390): per-apex custom-domain registration, gated by the
    # SAME per-apex switch as the DNS flip (var.apex_cutover_live_keys). Adding
    # the brand apex to the app's domain list is what makes DO's App Platform
    # edge ROUTE that Host instead of 403ing it fail-closed — the free-plan-safe
    # replacement for the Cloudflare Origin "Host Header override" rule, which is
    # Enterprise-only entitlement ("not entitled to use the HostHeader override"
    # on the Free brand zones). CF proxies the apex CNAME → *.ondigitalocean.app
    # ingress, so CF connects with SNI = the ondigitalocean.app host (valid wild-
    # card cert, Full-strict-safe) and forwards Host = <apex>; DO routes by that
    # Host once it is registered here. type=PRIMARY, zone omitted (DNS lives in
    # Cloudflare, not DO-managed). Enable a key ONLY in lockstep with flipping
    # that apex's DNS to the proxied CNAME (canary: ggg → hub/goldberry/nursery).
    dynamic "domain" {
      for_each = contains(var.apex_cutover_live_keys, each.key) ? [1] : []
      content {
        name = local.tenants[each.key]
        type = "PRIMARY"
      }
    }
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
