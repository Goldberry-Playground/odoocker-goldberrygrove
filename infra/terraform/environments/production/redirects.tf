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
# NOTE (one-time token update): the account CF token needs the
# "Dynamic URL Redirects: Edit" zone permission for these two zones — the current
# scope (DNS + Zone read + Zone Settings + SSL) predates rulesets and 403s here
# until updated.
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
