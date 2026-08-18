# -- Per-brand-zone origin TLS mode (Full → Full(strict)) ----------------------
# GOL-1551 (split from GOL-1545 Phase-2 finalize).
#
# The zone SSL/TLS mode governs ONLY the Cloudflare-edge → origin leg, and ONLY
# for PROXIED (orange-cloud) records. `full` encrypts edge→origin but does NOT
# validate the origin cert; `strict` (Full(strict)) additionally requires every
# proxied origin to present a cert trusted by CF — a public CA OR a Cloudflare
# Origin CA cert. DNS-only (grey-cloud) records bypass CF entirely and are
# unaffected by this setting. Flipping is reversible in seconds (strict→full).
#
# This was previously NOT terraform-managed (any dashboard flip was undocumented
# snowflake drift). `cloudflare_zone_settings_override` only manages the settings
# explicitly declared in its `settings {}` block, so declaring just `ssl` leaves
# every other zone setting untouched. The account CF token already carries "Zone
# Settings + SSL" scope (see apex-cutover.tf token-scope note) — no scope bump.
#
# ORIGIN AUDIT (2026-08-16, GOL-1551 — every proxied origin verified strict-safe
# via CF API + SNI cert probe, then live-flipped + verified apex/blog/store/odoo
# match Full baseline exactly, no 526):
#
#   hub (gatheringatthegrove.com)  — STRICT
#     apex→App Platform (GTS) · blog/odoo/rum→CF Origin CA (Caddy, serves for
#     Full(strict) by design; :443 CF-IP-locked so a direct probe fails by
#     design) · agenticos→App Platform (GTS) · assets→DO Spaces CDN (LE wildcard
#     *.gatheringatthegrove.com) · discord/paperclip→cfargotunnel (CF-terminated).
#     email.mg.*→mailgun is proxied but a 3-level host NOT covered by Universal
#     SSL, so edge TLS fails before origin-pull ⇒ strict is a no-op for it.
#   goldberry (goldberrygrove.farm) — STRICT
#     apex→App Platform (GTS) · blog→CF Origin CA · store→Shopify presents a
#     Google Trust Services cert for CN=store.goldberrygrove.farm (exact host,
#     trusted CA) ⇒ strict-safe; its 404 is an app-level Shopify response, not
#     TLS. email.mg proxied — same edge-gap no-op as hub.
#   nursery (atthegrovenursery.com) — STRICT
#     ONLY blog.* is proxied (CF Origin CA). apex/www/email.mg are grey-cloud
#     (DNS-only) ⇒ SSL mode irrelevant to them.
#   ggg (woodworkingeorge.com) — STRICT (2026-08-18, GOL-1551 tail)
#     apex→App Platform (GTS) + blog→CF Origin CA are strict-safe. The lone
#     blocker was www.woodworkingeorge.com → parkingpage.namecheap.com (proxied,
#     no strict-valid origin cert, 525 even on Full). RESOLVED by a CF edge
#     redirect www→apex (cloudflare_ruleset.www_apex_redirect in redirects.tf):
#     the http_request_dynamic_redirect phase fires BEFORE origin-pull, so the
#     parking-page origin is never contacted and its (missing) cert is moot.
#     Live-flipped + verified 2026-08-18: apex 200, www 301→apex (path+query
#     preserved), blog 301 — no 526. email.mg proxied — same edge-gap no-op as
#     the other zones.

variable "zone_ssl_mode" {
  description = "Per-brand-zone Cloudflare origin-facing SSL/TLS mode. 'strict' = Full(strict) (CF validates the origin cert is trusted); 'full' = Full (encrypted but unvalidated). Set a zone to 'strict' ONLY after confirming EVERY proxied origin in it presents a strict-valid cert (public CA or CF Origin CA) — see the GOL-1551 audit in tls.tf. Reversible in seconds. This committed default is the SOURCE OF TRUTH: prod-plan-guard and every var-less apply plan against it, so it MUST equal the zones' live modes."
  type        = map(string)

  default = {
    hub       = "strict" # GOL-1551: all proxied origins verified strict-safe + live-flipped 2026-08-16
    goldberry = "strict" # GOL-1551: store(Shopify GTS)/blog(Origin CA)/apex(App Platform) verified
    nursery   = "strict" # GOL-1551: only blog.* proxied (CF Origin CA); rest grey-cloud
    ggg       = "strict" # GOL-1551: www.* unblocked via edge redirect (redirects.tf); apex(App Platform)/blog(Origin CA) verified + live-flipped 2026-08-18
  }

  validation {
    condition     = alltrue([for m in values(var.zone_ssl_mode) : contains(["full", "strict"], m)])
    error_message = "zone_ssl_mode values must be 'full' or 'strict'."
  }

  validation {
    condition     = alltrue([for k in keys(var.zone_ssl_mode) : contains(["hub", "goldberry", "ggg", "nursery"], k)])
    error_message = "zone_ssl_mode keys may only be tenant keys: hub, goldberry, ggg, nursery."
  }
}

resource "cloudflare_zone_settings_override" "brand_tls" {
  for_each = local.tenants

  zone_id = data.cloudflare_zone.brand[each.key].id

  settings {
    ssl = var.zone_ssl_mode[each.key]
  }
}
