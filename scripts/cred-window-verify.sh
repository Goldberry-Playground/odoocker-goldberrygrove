#!/usr/bin/env bash
# cred-window-verify.sh -- per-consumer verification for the December 2026
# batched credential-rotation window (GOL-3802). Runbook:
#   docs/RUNBOOK-dec-2026-credential-window-GOL-3802.md
#
# Prints ONLY liveness/scope evidence (HTTP status codes, key fingerprints,
# field presence). It never prints a secret value and never writes anything
# to DigitalOcean, Cloudflare, Stripe or Paperclip -- every call is a GET.
#
# Usage:
#   bash scripts/cred-window-verify.sh [do|cf|r2|ssh|paperclip|stripe|all]
#
# Requires: `op` (1Password CLI, service account or signed-in user), curl,
# python3, ssh-keygen. Exit 0 always -- read the table; the runbook says what
# each cell must show after the window.
set -u

SECTION="${1:-all}"

say() { printf '%s\n' "$*"; }

# http_code <url> <bearer>
http_code() {
  curl -s -o /dev/null -w '%{http_code}' -m 20 -H "Authorization: Bearer $2" "$1"
}

op_read() {
  op read "$1" 2>/dev/null || true
}

# ── DigitalOcean: one row per vault field, one column per scope family ──────
do_probe() {
  local ref="$1" tok
  tok="$(op_read "$ref")"
  if [ -z "$tok" ]; then
    printf '%-62s | %s\n' "$ref" "FIELD MISSING / unreadable"
    return
  fi
  local out="" p c
  for p in account droplets firewalls domains databases apps "spaces/keys"; do
    c="$(http_code "https://api.digitalocean.com/v2/$p?per_page=1" "$tok")"
    out="$out $(printf '%s=%s' "${p%%/*}" "$c")"
  done
  printf '%-62s |%s  prefix=%s\n' "$ref" "$out" "${tok:0:12}…"
}

section_do() {
  say "## DigitalOcean tokens (expect: distinct prefixes per field; 401 = dead; 403 = out of scope)"
  do_probe "op://Goldberry Grove - Admin/Grove Infra/do_token"
  do_probe "op://Goldberry Grove - Admin/Grove Infra/do_token_teardown"
  do_probe "op://Goldberry Grove - Admin/Grove Infra/do_token_firewall_ci"
  do_probe "op://Goldberry Grove - Admin/Grove Infra/do_token_scoped"
  do_probe "op://Goldberry Grove - Admin/Grove Infra/do_token_grove_sites_ro"
  do_probe "op://Grove Prod/odoocker/DIGITALOCEAN_TOKEN"
  do_probe "op://Goldberry Grove - Admin/AgenticOS Infra/do_token"
  say ""
  say "## DO-registered SSH keys (names + MD5 fingerprints; expect grove-prod-admin present after item 1)"
  local tok
  tok="$(op_read "op://Goldberry Grove - Admin/Grove Infra/do_token")"
  if [ -n "$tok" ]; then
    curl -s -m 20 -H "Authorization: Bearer $tok" \
      "https://api.digitalocean.com/v2/account/keys?per_page=50" \
      | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print("  (could not parse /v2/account/keys)"); sys.exit(0)
for k in d.get("ssh_keys",[]):
    print("  %-26s %s" % (k["name"], k["fingerprint"]))'
  fi
}

# ── Cloudflare: /user/tokens/verify per field ───────────────────────────────
cf_probe() {
  local ref="$1" tok
  tok="$(op_read "$ref")"
  if [ -z "$tok" ]; then
    printf '%-62s | %s\n' "$ref" "FIELD MISSING / unreadable"
    return
  fi
  # User-owned tokens (cfut_) verify at /user/tokens/verify; ACCOUNT-owned
  # tokens (cfat_) only verify at /accounts/<id>/tokens/verify and answer
  # "Invalid API Token" on the user endpoint even when perfectly healthy.
  local verify_url="https://api.cloudflare.com/client/v4/user/tokens/verify" acct
  case "$tok" in
    cfat_*)
      acct="$(curl -s -m 20 -H "Authorization: Bearer $tok" \
        "https://api.cloudflare.com/client/v4/accounts?per_page=1" \
        | python3 -c 'import sys,json
try:
    r=json.load(sys.stdin).get("result") or []; print(r[0]["id"] if r else "")
except Exception:
    print("")')"
      [ -n "$acct" ] && verify_url="https://api.cloudflare.com/client/v4/accounts/$acct/tokens/verify"
      ;;
  esac
  local verdict
  verdict="$(curl -s -m 20 -H "Authorization: Bearer $tok" "$verify_url" \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print("unparseable"); sys.exit(0)
r=d.get("result") or {}
if d.get("success"):
    print("active=%s expires_on=%s" % (r.get("status"), r.get("expires_on") or "never"))
else:
    print("INVALID: " + "; ".join(e.get("message","") for e in d.get("errors",[])))')"
  local zones
  zones="$(curl -s -m 20 -H "Authorization: Bearer $tok" \
    "https://api.cloudflare.com/client/v4/zones?per_page=50" \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(len(d.get("result") or []))
except Exception:
    print("?")')"
  printf '%-62s | %s zones=%s prefix=%s len=%s\n' "$ref" "$verdict" "$zones" "${tok:0:6}…" "${#tok}"
}

section_cf() {
  say "## Cloudflare tokens (expect: prod token active + zones=4; QA token zones=1; dead fields deleted)"
  cf_probe "op://Grove Prod/Cloudflare API Token/credential"
  cf_probe "op://Goldberry Grove - Admin/Grove Infra/cf_zone_api_key"
  cf_probe "op://Grove Prod/odoocker/CLOUDFLARE_API_TOKEN"
  cf_probe "op://Goldberry Grove - Admin/Grove Infra/account_cloudflare_api_token"
  cf_probe "op://Goldberry Grove - Admin/Grove Infra/cloudflare_analytics_read_token"
}

# ── R2: field presence only; the probe itself is the GOL-2760 runbook ───────
section_r2() {
  say "## R2 state-lock probe credentials (expect: both fields present after item 3)"
  local f v
  for f in r2_access_key_id r2_secret_access_key; do
    v="$(op_read "op://Goldberry Grove - Admin/Grove Infra/$f")"
    if [ -n "$v" ]; then printf '  %-24s present (len=%s)\n' "$f" "${#v}"; else printf '  %-24s MISSING\n' "$f"; fi
  done
  say "  then: docs/RUNBOOK-r2-state-lock-probe-GOL-2760.md Step 1 (exit 0/1 = verdict, exit 3 = creds wrong)"
}

# ── SSH: fingerprints only ──────────────────────────────────────────────────
fp_of_vault_pub() {
  local v
  v="$(op_read "$1")"
  [ -n "$v" ] && printf '%s' "$v" | ssh-keygen -lf /dev/stdin 2>/dev/null | awk '{print $2}'
}

section_ssh() {
  say "## SSH keys (fingerprints only)"
  local f
  if [ -f "$HOME/.ssh/grove_qa_admin" ]; then
    f="$(ssh-keygen -lf "$HOME/.ssh/grove_qa_admin" 2>/dev/null | awk '{print $2}')"
    say "  agent host ~/.ssh/grove_qa_admin PRESENT  $f   <- item 1 expects this file GONE"
  else
    say "  agent host ~/.ssh/grove_qa_admin absent (good)"
  fi
  say "  vault grove_qa_admin_ssh_public_key   $(fp_of_vault_pub 'op://Goldberry Grove - Admin/Grove Infra/grove_qa_admin_ssh_public_key')"
  say "  vault grove_prod_admin_ssh_public_key $(fp_of_vault_pub 'op://Goldberry Grove - Admin/Grove Infra/grove_prod_admin_ssh_public_key')  <- item 1 expects a value (public half only)"
  if [ -f "$HOME/.config/doctl/config.yaml" ]; then
    say "  agent host ~/.config/doctl/config.yaml PRESENT  <- item 2 expects this file GONE"
  else
    say "  agent host ~/.config/doctl/config.yaml absent (good)"
  fi
}

# ── Paperclip board key: auth-only GET ──────────────────────────────────────
section_paperclip() {
  say "## Paperclip board key (expect 200 with the new value; the OLD value must 401)"
  local tok url host
  tok="$(op_read "op://Goldberry Grove - Admin/AgenticOS Infra/paperclip_board_key")"
  if [ -z "$tok" ]; then say "  paperclip_board_key FIELD MISSING / unreadable"; return; fi
  url="${PAPERCLIP_API_URL:-https://paperclip.gatheringatthegrove.com}"
  host="${url#https://}"; host="${host#http://}"; host="${host%%/*}"
  # In-cluster path bypasses Cloudflare Access; the Host header is mandatory.
  printf '  GET /api/companies/%s -> %s\n' "${PAPERCLIP_COMPANY_ID:-<set PAPERCLIP_COMPANY_ID>}" \
    "$(curl -s -o /dev/null -w '%{http_code}' -m 30 -H "Host: $host" -H "Authorization: Bearer $tok" \
       "http://paperclip-server:3100/api/companies/${PAPERCLIP_COMPANY_ID:-}")"
}

# ── Stripe: /v1/account per tenant key (read-only; prints acct id + livemode) ─
stripe_probe() {
  local label="$1" ref="$2" key
  key="$(op_read "$ref")"
  if [ -z "$key" ]; then printf '  %-10s FIELD MISSING (%s)\n' "$label" "$ref"; return; fi
  curl -s -m 20 -u "$key:" https://api.stripe.com/v1/account \
    | python3 -c 'import sys,json
lab=sys.argv[1]
try:
    d=json.load(sys.stdin)
except Exception:
    print("  %-10s unparseable" % lab); sys.exit(0)
if "error" in d:
    print("  %-10s ERROR %s" % (lab, d["error"].get("message","")))
else:
    print("  %-10s acct=%s" % (lab, d.get("id")))' "$label"
}

section_stripe() {
  say "## Stripe per-tenant keys (expect 3 DISTINCT acct ids: goldberry acct_1RbD4B…, nursery acct_16cKwA…, ggg acct_1Ttsj9…)"
  stripe_probe goldberry "op://Grove Prod/stripe/goldberry_stripe_key"
  stripe_probe nursery   "op://Grove Prod/stripe/atthegrovenursery_stripe_key"
  stripe_probe ggg       "op://Grove Prod/stripe/ggg_stripe_key"
}

case "$SECTION" in
  do) section_do ;;
  cf) section_cf ;;
  r2) section_r2 ;;
  ssh) section_ssh ;;
  paperclip) section_paperclip ;;
  stripe) section_stripe ;;
  all)
    section_do; say ""; section_cf; say ""; section_r2; say ""; section_ssh; say ""
    section_paperclip; say ""; section_stripe
    ;;
  *)
    say "usage: $0 [do|cf|r2|ssh|paperclip|stripe|all]" >&2
    exit 2
    ;;
esac
