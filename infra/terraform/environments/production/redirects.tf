# -- Apex → blog.* content-asset redirect (POST-LAUNCH, narrow 301) --------------
#
# Post-cutover (GOL-287 / GOL-1279 / GOL-1282), the hub + goldberry apexes are
# served by their App Platform Next.js storefronts (proxied CNAME →
# *.ondigitalocean.app + a Host-override Origin Rule). The apex ROOT and every
# storefront route MUST reach that origin, so this rule is deliberately NARROW:
# it 301-redirects ONLY Ghost's embedded-asset path `/content/*` (images / media
# / files baked into historical post HTML) to blog.<apex>, and lets every other
# path fall through to origin selection.
#
# This REPLACES the pre-launch blanket rule
#   (http.host eq <apex>) -> 302  https://blog.<apex><path>
# which matched EVERY path — including `/` (the storefront homepage). Because the
# http_request_dynamic_redirect phase runs BEFORE origin selection, that blanket
# rule shadows the app entirely: pointing apex DNS at App Platform does nothing
# while it is enabled. The old runbook Step 3 ("bump 302 → 301") would have made
# that shadowing PERMANENT and browser-cached — poisoning the flagship apex. The
# correct fix is to narrow the rule, not bump its status code. See
# docs/RUNBOOK-apex-launch-cutover.md Step 3.
#
# `/content/` is unambiguously Ghost (never a storefront route), so it is safe to
# redirect. `/assets/`, `/public/`, and bare post slugs are NOT included: theme
# assets collide with Next.js static paths, and legacy post slugs (apex/<slug>)
# are indistinguishable from storefront routes at the edge — a post-path 301 map
# would need an explicit slug allow-list and is deferred to GOL-1284 (old post
# links 404 on the apex until it ships; accepted at launch).
#
# 301 (permanent) is correct now: this is the final headless topology, not a
# temporary QA state, and `/content/*` will only ever live on blog.<apex>.
#
# `var.blog_apex_redirects_enabled` gates the rule so Terra enables it in the
# cutover window (per-zone), not on merge. blog.<apex> must be serving Ghost
# (blogs-droplet url-flip, Step 2) before enabling, else these targets 404.
#   op run --env-file=.env.op -- terraform apply -var=blog_apex_redirects_enabled=true
#
# NOTE (token scope): these rulesets need the CF token's zone-scoped redirect
# permission on these two zones. Cloudflare RENAMED this permission group to
# "Single Redirect" in the dashboard (it now backs the Single Redirects product,
# implemented by the same `http_request_dynamic_redirect` phase) — the old
# "Dynamic URL Redirects" / "Firewall Services" label no longer appears in the
# zone dropdown, so grant "Single Redirect: Edit" when minting/scoping the token.
# GRANTED 2026-08-28 (GOL-1770) on all three managed zones; a token missing it
# fails plan here with "request is not authorized". DNS + Zone read + Zone
# Settings + SSL alone predate rulesets and 403 on this phase.
#
# This ruleset owns the ENTIRE http_request_dynamic_redirect phase for each zone:
# any hand-created dashboard redirect rules in that phase are removed on apply.
# That is intended — redirects live here, not in the UI.

resource "cloudflare_ruleset" "blog_apex_redirect" {
  for_each = toset(["hub", "goldberry"])

  zone_id = data.cloudflare_zone.brand[each.key].id
  # `name` is ForceNew on cloudflare_ruleset (provider ~>4.40): editing it would
  # REPLACE these two already-applied (disabled) live rulesets, tripping
  # prod-plan-guard. The functional narrowing below (expression/status/description)
  # all applies IN-PLACE, so the name is deliberately frozen to its live value —
  # stale label, zero blast radius. Rename deferred to the next legitimate replace
  # (e.g. the GOL-1284 post-path map). See GOL-1283 review.
  name  = "blog apex redirects (302, pre-launch)"
  kind  = "zone"
  phase = "http_request_dynamic_redirect"

  rules {
    enabled     = var.blog_apex_redirects_enabled
    description = "301 ${local.tenants[each.key]}/content/* -> blog.${local.tenants[each.key]}/content/* (embedded Ghost assets only; apex root reaches the app)"
    expression  = "(http.host eq \"${local.tenants[each.key]}\" and starts_with(http.request.uri.path, \"/content/\"))"
    action      = "redirect"

    action_parameters {
      from_value {
        status_code = 301
        target_url {
          expression = "concat(\"https://blog.${local.tenants[each.key]}\", http.request.uri.path)"
        }
        preserve_query_string = true
      }
    }
  }
}

# -- www.woodworkingeorge.com → apex canonical redirect (GOL-1551 tail) ----------
#
# The ggg zone's lone Full(strict) blocker was www.woodworkingeorge.com, a
# proxied CNAME → parkingpage.namecheap.com with NO strict-valid origin cert
# (525 even on Full — the parking origin never completed a CF→origin handshake).
#
# Rather than drop the record (NXDOMAIN for www) or accept a permanent 526, we
# 301 www → the canonical apex at the CF edge. The http_request_dynamic_redirect
# phase runs BEFORE origin selection/pull, so CF answers the redirect itself and
# never contacts the parking origin — its (missing/invalid) cert becomes moot,
# which is exactly what unblocks ggg → Full(strict) (see tls.tf). Path + query
# are preserved (www.../collections/all?x=1 → apex/collections/all?x=1).
#
# ggg is NOT in cloudflare_ruleset.blog_apex_redirect (hub/goldberry only), so
# this is a separate resource owning ggg's dynamic_redirect phase entrypoint.
# Same phase-ownership caveat applies: this ruleset owns the ENTIRE
# http_request_dynamic_redirect phase for the ggg zone — any hand-created
# dashboard rule in that phase is removed on apply (intended).
#
# Live-created via the CF API + flipped ggg→strict on 2026-08-18 (verified: apex
# 200, www 301→apex, blog 301, no 526); this codifies the source of truth. The
# `name` must match the live ruleset exactly ("www apex canonical redirect") —
# `name` is ForceNew on cloudflare_ruleset (provider ~>4.40), and this resource
# is not yet in prod state (see the reconcile note in the GOL-1551 PR). On the
# reconcile apply it must be `terraform import`ed, not re-created (a second
# dynamic_redirect entrypoint would conflict).
resource "cloudflare_ruleset" "www_apex_redirect" {
  zone_id = data.cloudflare_zone.brand["ggg"].id

  name  = "www apex canonical redirect"
  kind  = "zone"
  phase = "http_request_dynamic_redirect"

  rules {
    enabled     = true
    description = "301 www.${local.tenants["ggg"]} -> ${local.tenants["ggg"]} (canonical apex; fires at edge before origin-pull, unblocks Full(strict)) [GOL-1551]"
    expression  = "(http.host eq \"www.${local.tenants["ggg"]}\")"
    action      = "redirect"

    action_parameters {
      from_value {
        status_code = 301
        target_url {
          expression = "concat(\"https://${local.tenants["ggg"]}\", http.request.uri.path)"
        }
        preserve_query_string = true
      }
    }
  }
}
