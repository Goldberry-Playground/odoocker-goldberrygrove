###############################################################################
# cloudflare-policy -- account-wide Cloudflare edge policy.
#
# Currently: geo-blocking. One WAF custom rule per zone that BLOCKS all
# traffic whose source country is in var.blocked_countries (CN + RU as of
# 2026-07-04). Free-plan zones get 5 custom rules; this uses 1 per zone.
#
# Scope notes, so nobody over-trusts this:
#   - Applies to PROXIED (orange-cloud) hostnames only. DNS-only records
#     bypass Cloudflare entirely and get no protection here.
#   - The grove-assets raw CDN hostname (grove-assets.nyc3.cdn.
#     digitaloceanspaces.com) is DO's own edge, not a hostname in these
#     zones -- traffic to it is NOT geo-blocked. The vanity assets host
#     (assets.<apex>, proxied + Worker) IS covered: this rule evaluates
#     in the http_request_firewall_custom phase, before Worker routes run.
#   - App Platform apps' *.ondigitalocean.app default ingresses are also
#     outside these zones; their qa-l3 vanity hosts live in the DO-delegated
#     qa-l3 zone, which Cloudflare never sees. Level 3 QA is therefore NOT
#     geo-blocked -- acceptable for QA; revisit for prod cutover (ADR-007
#     Phase 6) when prod hostnames land on proxied Cloudflare records.
#
# Action is `block` (hard 403 from the CF edge) rather than
# `managed_challenge` per operator decision 2026-07-04: these businesses
# have zero legitimate CN/RU audience, so the friction-vs-protection
# trade-off of a challenge buys nothing.
###############################################################################

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

data "cloudflare_zone" "zones" {
  for_each = var.zone_names
  name     = each.value
}

locals {
  # `in` set syntax wants space-separated quoted values: {"CN" "RU"}
  country_set = join(" ", [for c in sort(tolist(var.blocked_countries)) : format("%q", c)])
}

resource "cloudflare_ruleset" "geo_block" {
  for_each = data.cloudflare_zone.zones

  zone_id     = each.value.id
  name        = "Grove edge policy"
  description = "Managed by TF (environments/cloudflare-policy). Do not edit in the dashboard -- changes get reverted on next apply."
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules {
    action      = "block"
    expression  = "(ip.geoip.country in {${local.country_set}})"
    description = "Block ${join("+", sort(tolist(var.blocked_countries)))} traffic (bot/scanner noise; no legitimate audience)"
    enabled     = true
  }
}

###############################################################################
# Odoo product-image edge cache (GOL-93)
#
# The prod Odoo droplet is a small s-2vcpu-4gb box. grove-sites' image resolver
# (packages/odoo-client/src/images.ts, grove-sites PR #33) sends every
# /web/image/* request straight to the Odoo host by design -- product photos,
# thumbnails, all ir.attachment binaries. Unbuffered, that traffic lands on the
# origin droplet and competes with the ERP/API workload.
#
# This rule caches /web/image/* at the Cloudflare edge with a long TTL so the
# droplet serves each image once (per PoP) and Cloudflare fields the rest.
# Odoo's /web/image URLs are content-addressed (they carry a write-date/hash
# token), so a long edge TTL is safe: when an image changes its URL changes,
# which is a natural cache-bust -- no purge needed.
#
# Cache rules live in the `http_request_cache_settings` phase -- a DIFFERENT
# ruleset from the geo_block above (`http_request_firewall_custom`), so both
# coexist (Cloudflare allows one ruleset per zone PER PHASE).
#
# Scope: gatheringatthegrove.com only (the Odoo/ERP zone). The record
# odoo.gatheringatthegrove.com must be PROXIED (orange-cloud) for this to take
# effect -- that proxied record lands with the Phase-6 prod cutover. Until
# then this rule is authored-but-inert (matches nothing), which is the correct
# pre-launch state for a blocker.
#
# Verify after cutover:
#   curl -sI 'https://odoo.gatheringatthegrove.com/web/image/...' | grep -i cf-cache-status
#   -> expect MISS on first hit, HIT thereafter (never DYNAMIC/BYPASS).
###############################################################################

resource "cloudflare_ruleset" "odoo_image_cache" {
  zone_id     = data.cloudflare_zone.zones["gatheringatthegrove.com"].id
  name        = "Grove Odoo image edge cache"
  description = "Managed by TF (environments/cloudflare-policy). Long edge cache for Odoo /web/image/* so product-photo traffic stays off the small prod Odoo droplet (GOL-93). Do not edit in the dashboard -- reverted on next apply."
  kind        = "zone"
  phase       = "http_request_cache_settings"

  rules {
    action = "set_cache_settings"
    action_parameters {
      cache = true

      # Force a long EDGE cache regardless of what Odoo sends. Odoo's
      # /web/image URLs are content-addressed, so stale content can't be
      # served under a changed URL.
      edge_ttl {
        mode    = "override_origin"
        default = 2592000 # 30 days
      }

      # Leave the BROWSER TTL to whatever Odoo declares (it already sets
      # sensible Cache-Control + ETag on image responses). We only want to
      # offload the origin, not dictate client caching.
      browser_ttl {
        mode = "respect_origin"
      }

      # Deliberately NO custom cache_key: keep Cloudflare's default key, which
      # INCLUDES the query string. Odoo appends a `?unique=<write-date>` token
      # to product-image URLs when content changes -- that token is the
      # cache-buster, so it must stay in the cache key or a replaced image
      # would serve stale for the full edge TTL.
    }
    expression  = "(http.host eq \"odoo.gatheringatthegrove.com\" and starts_with(http.request.uri.path, \"/web/image/\"))"
    description = "Cache /web/image/* at edge for 30d; browser TTL respects origin"
    enabled     = true
  }
}
