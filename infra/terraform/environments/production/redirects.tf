# -- Blog apex 302 redirects (EOM-July QA phase) ---------------------------------
#
# goldberrygrove.farm and gatheringatthegrove.com currently serve Ghost at the
# APEX. Once the blogs droplet's Ghost `url` flips to blog.* (cloud-init change
# in this same PR), the apexes must 302 to blog.* so existing readers and
# content-embedded asset URLs keep resolving.
#
# 302, deliberately NOT 301: the apexes' permanent home is the headless App
# Platform frontends (late-August cutover). Browsers cache 301s aggressively —
# a 301 here would strand July visitors on blog.* after the cutover. At the
# prod cutover these rules are REPLACED by: apex DNS → App Platform, a 301 map
# for legacy post paths, and a narrow permanent apex/content/* → blog.*/content/*
# asset rule (tracked in the cutover runbook).
#
# Rules are created DISABLED (var.blog_apex_redirects_enabled = false): blog.*
# vhosts 404 until the droplet-replace apply completes, so enabling is a
# SEPARATE, deliberate second apply after blog.* verifies healthy:
#   op run --env-file=.env.op -- terraform apply -var=blog_apex_redirects_enabled=true
#
# NOTE (one-time token update): the account CF token needs the
# "Dynamic URL Redirects: Edit" zone permission added for these two zones —
# the current scope (DNS + Zone read + Zone Settings + SSL) predates rulesets
# and will 403 on this resource until updated.
#
# This ruleset owns the ENTIRE http_request_dynamic_redirect phase for each
# zone: any hand-created dashboard redirect rules in that phase would be
# removed on apply. That is intended — redirects live here, not in the UI.

resource "cloudflare_ruleset" "blog_apex_redirect" {
  for_each = toset(["hub", "goldberry"])

  zone_id = data.cloudflare_zone.brand[each.key].id
  name    = "blog apex redirects (302, pre-launch)"
  kind    = "zone"
  phase   = "http_request_dynamic_redirect"

  rules {
    enabled     = var.blog_apex_redirects_enabled
    description = "302 ${local.tenants[each.key]}/* -> blog.${local.tenants[each.key]}/* until headless cutover"
    expression  = "(http.host eq \"${local.tenants[each.key]}\")"
    action      = "redirect"

    action_parameters {
      from_value {
        status_code = 302
        target_url {
          expression = "concat(\"https://blog.${local.tenants[each.key]}\", http.request.uri.path)"
        }
        preserve_query_string = true
      }
    }
  }
}
