# -- Apex → App Platform Host-override Origin Rule (GOL-287 / GOL-1279) ---------
#
# The launch cutover flips each brand apex from Ghost (blogs droplet, A record)
# to its App Platform Next.js storefront (proxied CNAME → *.ondigitalocean.app).
# Under Full(strict), Cloudflare connects to the origin with SNI = the CNAME
# target and forwards a Host header defaulting to the VISITOR hostname (the
# apex). DO's App Platform edge routes purely by a recognized Host and FAILS
# CLOSED with 403 (server: cloudflare, no x-do-app-origin) on any Host it does
# not own. So an Origin Rule that rewrites the Host header to the app's default
# ingress is MANDATORY, not optional (recipe proven in GOL-285; runbook §Recipe).
#
# WHY THIS IS CODE (previously deferred at apps.tf "Apex cutover"): the Origin
# Rule is the one purely-codifiable cutover lever (the DNS leg stays a manual
# per-apex edit for instant rollback; SSL Full-strict is a one-click zone
# toggle). Codifying it makes the window a reviewed `terraform apply`, not a
# live click-op, and keeps the Host targets self-correcting (see below).
#
# --- PER-APEX GATING (this is load-bearing, do not collapse to a single bool) --
# A "Set Host Header" Origin Rule fires for EVERY request matching
# `http.host eq <apex>`, at the http_request_origin phase, BEFORE origin
# selection — independent of what the apex DNS currently resolves to. So if this
# rule is enabled for an apex whose DNS still points at Ghost (blogs droplet),
# Cloudflare would connect to the Ghost origin but send Host = <app>.ondigital-
# ocean.app, which Ghost does not serve → the apex BREAKS before the flip.
# Therefore each apex's rule must be created IN LOCKSTEP with that apex's DNS
# flip, never ahead of it. `var.apex_cutover_live_keys` is the per-apex switch:
# add a key only after (or together with) flipping that apex's DNS to the CNAME.
#
# --- WINDOW SEQUENCE, per apex (runbook Step 1) --------------------------------
#   1. Flip apex DNS A→blogs-IP  ⟶  proxied CNAME → <app>.ondigitalocean.app
#      (manual CF edit — instantly reversible; the one-way-door's reversible half).
#   2. Confirm zone SSL/TLS = Full (strict).
#   3. Enable this rule for that apex:
#        op run --env-file=.env.op -- terraform apply \
#          -target='cloudflare_ruleset.apex_host_override' \
#          -var='apex_cutover_live_keys=["hub"]'          # then ["hub","goldberry"], …
#   4. Verify (Ada): curl -sSI https://<apex>/ → 200 + x-do-orig-status:200 + brand <title>.
# There is a seconds-long 403 gap between step 1 and step 3 (DNS says app, no
# Host override yet). That is expected and covered by DNS-revert rollback.
#
# ROLLBACK (per apex): revert that apex's DNS record to A→blogs-IP (instant), and
# drop its key from apex_cutover_live_keys on the next apply. Independent per apex.
#
# --- TOKEN SCOPE PREREQ (blocks the apply, not the merge) ----------------------
# Origin Rules live in the http_request_origin ruleset phase. The account CF
# token (1P `account_cloudflare_api_token`) currently carries DNS + Zone read +
# Zone Settings + SSL, which PREDATES rulesets and 403s on this resource. The
# token needs "Account/Zone → Config Rules (or Origin Rules): Edit" on the four
# brand zones before this can apply — same class of one-time scope bump the
# redirects.tf ruleset needs ("Dynamic URL Redirects: Edit"). Owner: CEO /
# account admin. Until then this resource stays gated off (empty for_each = no
# API call), so merging is inert.

locals {
  # Full ingress URL per tenant, read from live app state (NOT hardcoded): the
  # DO-assigned "-bpyrs"/"-efc9e" suffix changes if an app is recreated, so
  # deriving keeps the Host target correct across rebuilds. Shape:
  # "https://grove-hub-prod-bpyrs.ondigitalocean.app".
  apex_app_live_url = merge(
    { hub = digitalocean_app.hub.live_url },
    { for k, app in digitalocean_app.tenant : k => app.live_url },
  )

  # Bare host for the Host header (strip scheme + any trailing slash).
  apex_ingress_host = {
    for k, url in local.apex_app_live_url :
    k => trimsuffix(replace(replace(url, "https://", ""), "http://", ""), "/")
  }
}

resource "cloudflare_ruleset" "apex_host_override" {
  # Per-apex gate: only apexes listed in apex_cutover_live_keys get a rule, so it
  # is created in lockstep with that apex's DNS flip (see header). Empty default
  # ⇒ zero resources ⇒ no CF API call ⇒ merge is inert / needs no token bump.
  for_each = {
    for k, apex in local.tenants : k => apex
    if contains(var.apex_cutover_live_keys, k)
  }

  zone_id = data.cloudflare_zone.brand[each.key].id
  name    = "App Platform host override"
  kind    = "zone"
  phase   = "http_request_origin"

  rules {
    action = "route"
    action_parameters {
      host_header = local.apex_ingress_host[each.key]
    }
    # Apex only. www.<apex> is a separate follow-up: it must not be host-
    # overridden here unless its DNS is also flipped to the app, or it would
    # 403 the same way an early apex rule breaks Ghost.
    expression  = "(http.host eq \"${each.value}\")"
    description = "Rewrite Host → App Platform ingress (${local.apex_ingress_host[each.key]}) so DO edge routes; else 403. GOL-116/GOL-285/GOL-287/GOL-1279."
    enabled     = true
  }
}
