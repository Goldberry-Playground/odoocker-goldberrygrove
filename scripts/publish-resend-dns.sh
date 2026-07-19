#!/usr/bin/env bash
# Idempotently publish the Resend `send.<domain>` email-auth DNS records into
# Cloudflare (GOL-580 / GOL-465 Phase 3).
#
# Why a script and not Terraform: Resend generates the exact DKIM CNAME target
# and the feedback-smtp AWS region *at domain-add time*, so the values are not
# knowable ahead of standing up the account. This script reads a records table
# (see scripts/resend-records.example.tsv) that the operator fills in from the
# Resend dashboard, then upserts each row into Cloudflare idempotently (safe to
# re-run). It does NOT delete anything.
#
# Auth: needs a Cloudflare API token with DNS:Edit on the target zones. The
# account-scoped token in 1Password already covers all four Grove zones:
#   op://Goldberry Grove - Admin/Grove Infra/account_cloudflare_api_token
#
# Usage:
#   CLOUDFLARE_API_TOKEN=... scripts/publish-resend-dns.sh scripts/resend-records.tsv
#   # dry run (print planned upserts, change nothing):
#   DRY_RUN=1 CLOUDFLARE_API_TOKEN=... scripts/publish-resend-dns.sh <records.tsv>
#
# Records file format (TAB-separated, '#' comment lines ignored):
#   zone <TAB> type <TAB> name <TAB> content <TAB> priority
#   - zone     = the Cloudflare zone (apex), e.g. goldberrygrove.farm
#   - type     = TXT | CNAME | MX
#   - name     = FQDN, e.g. send.goldberrygrove.farm
#   - content  = record value (unquoted; TXT quoting handled by CF)
#   - priority = MX priority, or "-" for non-MX
# Rows whose content still contains TODO_FROM_RESEND are skipped with a warning.
set -euo pipefail

API="https://api.cloudflare.com/client/v4"
RECORDS_FILE="${1:?usage: publish-resend-dns.sh <records.tsv>}"
: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN (DNS:Edit on the Grove zones)}"
DRY_RUN="${DRY_RUN:-0}"

cf() { # cf <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$API$path" \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
      -H "Content-Type: application/json" --data "$body"
  else
    curl -sS -X "$method" "$API$path" \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
  fi
}

# jq-free JSON field extraction via python3 (always present in this repo's images)
jget() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

declare -A ZONE_ID
zone_id() {
  local zone="$1"
  if [[ -z "${ZONE_ID[$zone]:-}" ]]; then
    local resp; resp="$(cf GET "/zones?name=$zone&status=active")"
    local id; id="$(printf '%s' "$resp" | jget 'd["result"][0]["id"] if d.get("result") else ""')"
    [[ -n "$id" ]] || { echo "ERROR: zone not found or token lacks access: $zone" >&2; exit 1; }
    ZONE_ID[$zone]="$id"
  fi
  printf '%s' "${ZONE_ID[$zone]}"
}

upsert() {
  local zone="$1" type="$2" name="$3" content="$4" priority="$5"
  local zid; zid="$(zone_id "$zone")"

  # Build the record body. MX carries a priority; CNAME must not be proxied.
  local body
  if [[ "$type" == "MX" ]]; then
    body="$(python3 -c 'import json,sys; print(json.dumps({"type":sys.argv[1],"name":sys.argv[2],"content":sys.argv[3],"priority":int(sys.argv[4]),"ttl":300}))' "$type" "$name" "$content" "$priority")"
  else
    body="$(python3 -c 'import json,sys; print(json.dumps({"type":sys.argv[1],"name":sys.argv[2],"content":sys.argv[3],"ttl":300,"proxied":False}))' "$type" "$name" "$content")"
  fi

  # Look for an existing record of the same type+name to update in place.
  local existing rid
  existing="$(cf GET "/zones/$zid/dns_records?type=$type&name=$name")"
  rid="$(printf '%s' "$existing" | jget 'd["result"][0]["id"] if d.get("result") else ""')"

  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [dry-run] %-5s %-45s -> %s%s\n' "$type" "$name" "$content" \
      "$([[ -n "$rid" ]] && echo "  (update $rid)" || echo "  (create)")"
    return
  fi

  local out ok
  if [[ -n "$rid" ]]; then
    out="$(cf PUT "/zones/$zid/dns_records/$rid" "$body")"
  else
    out="$(cf POST "/zones/$zid/dns_records" "$body")"
  fi
  ok="$(printf '%s' "$out" | jget 'd.get("success")')"
  if [[ "$ok" == "True" ]]; then
    printf '  OK    %-5s %s\n' "$type" "$name"
  else
    printf '  FAIL  %-5s %s\n    %s\n' "$type" "$name" "$(printf '%s' "$out" | jget 'd.get("errors")')" >&2
    exit 1
  fi
}

echo "Publishing Resend DNS from $RECORDS_FILE (DRY_RUN=$DRY_RUN)"
skipped=0
while IFS=$'\t' read -r zone type name content priority; do
  [[ -z "${zone// }" || "${zone:0:1}" == "#" ]] && continue
  if [[ "$content" == *TODO_FROM_RESEND* ]]; then
    printf '  SKIP  %-5s %-45s (fill from Resend dashboard first)\n' "$type" "$name" >&2
    skipped=$((skipped+1)); continue
  fi
  upsert "$zone" "$type" "$name" "$content" "${priority:--}"
done < "$RECORDS_FILE"

if [[ "$skipped" -gt 0 ]]; then
  echo "WARNING: $skipped record(s) skipped (still TODO_FROM_RESEND). Domains will NOT verify until these are filled + published." >&2
  exit 2
fi
echo "Done. Now click 'Verify' on each domain in Resend and wait for 'Verified'."
