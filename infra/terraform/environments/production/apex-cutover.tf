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
# --- VALIDATION APEX: ggg goes first (GOL-1390) --------------------------------
# ggg (woodworkingeorge.com — SINGLE-g; this is the codified apex in
# main.tf local.tenants, NOT the double-g "woodworkinggeorge.com" that some
# runbook prose typo'd) is the canary/validation apex for this procedure: lowest
# revenue exposure, and it is ALREADY exhibiting the exact failure this rule
# fixes (apex connection-timeout / www 525 — an out-of-band DNS flip to the app
# with no Host-override rule). Prove the corrected sequence on ggg before the
# hub/goldberry/nursery flips.
#
# --- WINDOW SEQUENCE, per apex (runbook Step 1) --------------------------------
#   1. Flip apex DNS A→blogs-IP  ⟶  proxied CNAME → <app>.ondigitalocean.app
#      (manual CF edit — instantly reversible; the one-way-door's reversible half).
#      (For ggg the DNS may already be on the CNAME; if so this step is a no-op
#       confirm, and the apex is down ONLY for lack of steps 2-3.)
#   2. Confirm zone SSL/TLS = Full (strict).
#   3. Enable this rule for that apex, IN-WINDOW transient override via -var:
#        op run --env-file=.env.op -- terraform apply \
#          -target='cloudflare_ruleset.apex_host_override' \
#          -var='apex_cutover_live_keys=["ggg"]'      # canary; then add hub, goldberry, …
#   4. Verify (Ada): curl -sSI https://<apex>/ → 200 + x-do-orig-status:200 + brand <title>.
#   5. MANDATORY CONVERGENCE (do NOT skip — finding #1, review 2026-08-12):
#      commit the verified key into the COMMITTED default of
#      var.apex_cutover_live_keys (variables.tf) and open a PR. The -var in
#      step 3 is an ephemeral in-window override ONLY. If the committed default
#      is not advanced to match reality, the very next var-less `terraform apply`
#      — and prod-plan-guard.yml, which plans with the committed default and NO
#      -var — will PLAN DESTRUCTION of this live rule → the flipped apex 403s
#      fail-closed and the guard goes permanently red. The committed default is
#      the source of truth; -var only bridges the seconds between step 3 and the
#      merge of step 5.
# There is a seconds-long 403 gap between step 1 and step 3 (DNS says app, no
# Host override yet). That is expected and covered by the rollback below.
#
# --- ROLLBACK (per apex) — TWO MANDATORY STEPS, IN ORDER -----------------------
# Reverting DNS ALONE DOES NOT ROLL BACK (finding #2, review 2026-08-12): this
# rule fires on `http.host eq <apex>` at the http_request_origin phase
# INDEPENDENT of DNS, so while the key is still live Cloudflare keeps rewriting
# Host/SNI → <app>.ondigitalocean.app even against the reverted Ghost origin
# (vhost/TLS mismatch under Full-strict) → the apex STAYS DOWN. So:
#   R1. DESTROY this apex's rule first — drop its key and targeted-apply:
#         op run --env-file=.env.op -- terraform apply \
#           -target='cloudflare_ruleset.apex_host_override' \
#           -var='apex_cutover_live_keys=[<remaining live keys, this apex removed>]'
#       (and land the matching committed-default revert per step 5 above).
#   R2. THEN revert that apex's DNS record CNAME→A→blogs-IP (CF-proxied, ~instant).
# Independent per apex. Skipping R1 = the apex cannot be restored by DNS alone.
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

# --- SUPERSEDED 2026-08-14 (GOL-1390): mechanism was NOT viable on Free zones --
# The Cloudflare Origin "Set Host Header" override (http_request_origin route
# action) that used to live here is an ENTERPRISE-ONLY entitlement. Applying it
# on the brand zones (all "Free Website" plan) fails hard:
#     Error: error creating ruleset App Platform host override
#            not entitled to use the HostHeader override
# So the resource is removed (it was never in state — the create errored before
# any object was recorded, so there is nothing to destroy).
#
# The apex cutover is instead achieved the free-plan-safe way, proven live on ggg
# 2026-08-14: register the brand apex as a custom DOMAIN on the tenant's App
# Platform app (apps.tf `dynamic "domain"`, same var.apex_cutover_live_keys gate).
# Once registered, DO issues a public cert for the apex (SAN=<apex>) and its
# Cloudflare-fronted edge routes by SNI — so the CF-proxied apex CNAME →
# *.ondigitalocean.app resolves and serves 200 with NO Host-header rewrite and NO
# token/plan upgrade. Verified: `curl -sSI https://woodworkingeorge.com/` → 200 +
# x-do-orig-status:200 + brand <title>.
#
# --- CUTOVER SEQUENCE, per apex (the DNS + app-domain legs) --------------------
#   1. Add the apex key to var.apex_cutover_live_keys and
#      `terraform apply -target='digitalocean_app.tenant["<key>"]'` — registers
#      the domain on the app (in-place; no rebuild). Wait for the app domain to
#      reach phase=ACTIVE ("domain <apex> ready").
#   2. Flip apex DNS: proxied A→parking  ⟶  proxied CNAME → <app>.ondigitalocean-
#      .app (manual CF edit — instantly reversible; the one-way-door's reversible
#      half). Zone SSL can stay Full; DO's edge presents a valid apex cert so
#      Full(strict) is also safe for the apex, but flip strict only after
#      confirming every other proxied origin in the zone (e.g. blog.*) has a
#      trusted cert — SSL mode is zone-wide.
#   3. Verify: curl -sSI https://<apex>/ → 200 + x-do-orig-status:200 + brand
#      <title>. (First cold hit may 522 while the edge warms; steady state 200.)
#   4. CONVERGENCE: commit the verified key into the committed default of
#      var.apex_cutover_live_keys (variables.tf) — prod-plan-guard plans against
#      that default, so it must equal the set of apexes actually live.
#
# --- ROLLBACK (per apex) -------------------------------------------------------
# Reverting DNS alone is sufficient here (unlike the old Host-override rule, the
# domain{} block is inert for traffic that no longer resolves to the app):
#   R1. Revert that apex's DNS record CNAME → A → parking/Ghost (CF-proxied, ~instant).
#   R2. Optionally drop the apex key + `terraform apply` to de-register the domain
#       on the app (cosmetic; leaving it registered does not break the reverted apex).
