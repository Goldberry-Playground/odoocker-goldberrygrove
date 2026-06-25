#!/usr/bin/env bash
# qa-teardown-droplet.sh -- Destroy ALL droplets tagged env-qa via DO API.
#
# Side-steps TF entirely (the DO provider's hardcoded 1m destroy poll has bit
# us multiple times). Direct API DELETE + generous poll budget per droplet.
#
# Idempotent: if no env-qa droplets exist, exits 0 silently.
#
# Usage:
#   bash scripts/qa-teardown-droplet.sh
#   bash scripts/qa-teardown-droplet.sh --dry-run     # list, don't destroy
#
# Required env:
#   DIGITALOCEAN_TOKEN  Must have `droplet:delete` scope. The do_token_teardown
#                       PAT from 1P 'GoldberryGrove Infra' is the right token.
#
# Optional env:
#   POLL_MAX_SECONDS   Poll budget per droplet (default: 300)
#   POLL_INTERVAL      Seconds between polls (default: 5)
#
# Exit codes:
#   0  All env-qa droplets destroyed (or none existed)
#   1  Required env missing
#   2  At least one DELETE failed (likely scope issue -- check token)
#   3  At least one poll timed out (DO API genuinely slow)

set -euo pipefail

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi

if [ -z "${DIGITALOCEAN_TOKEN:-}" ]; then
  echo "ERROR: DIGITALOCEAN_TOKEN not set." >&2
  echo "  export DIGITALOCEAN_TOKEN=\$(op item get \"GoldberryGrove Infra\" --vault \"Goldberry Grove - Admin\" --fields label=do_token_teardown --reveal)" >&2
  exit 1
fi

POLL_MAX_SECONDS="${POLL_MAX_SECONDS:-300}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

echo "-- QA droplet teardown --"
list_json=$(curl -sf -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" "https://api.digitalocean.com/v2/droplets?tag_name=env-qa")
ids=$(echo "$list_json" | jq -r '.droplets[].id // empty')

if [ -z "$ids" ]; then
  echo "  (no env-qa droplets found -- nothing to destroy)"
  exit 0
fi

count=$(echo "$ids" | wc -l | tr -d ' ')
echo "  found $count droplet(s) to destroy"

if [ "$DRY_RUN" = "1" ]; then
  echo "$list_json" | jq -r '.droplets[] | "  DRY-RUN would destroy: \(.name) id=\(.id) ip=\(.networks.v4[0].ip_address // "(no ip)") created=\(.created_at)"'
  exit 0
fi

fail_destroy=0
fail_poll=0
for id in $ids; do
  echo "  -> droplet $id: issuing DELETE"
  http_code=$(curl -s -o /tmp/td.$$ -w '%{http_code}' \
    -X DELETE -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
    "https://api.digitalocean.com/v2/droplets/${id}")
  case "$http_code" in
    204|404)
      echo "     DELETE accepted (HTTP $http_code) -- polling"
      ;;
    403)
      echo "     FAIL: HTTP 403 (token lacks droplet:delete scope)" >&2
      fail_destroy=1
      continue
      ;;
    *)
      echo "     FAIL: HTTP $http_code -- response: $(cat /tmp/td.$$ 2>/dev/null)" >&2
      fail_destroy=1
      continue
      ;;
  esac
  rm -f /tmp/td.$$

  # Poll until 404 or budget exhausted
  start=$(date +%s)
  deadline=$((start + POLL_MAX_SECONDS))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
      "https://api.digitalocean.com/v2/droplets/${id}")
    if [ "$code" = "404" ]; then
      echo "     droplet $id confirmed destroyed (HTTP 404)"
      break
    fi
    sleep "$POLL_INTERVAL"
  done
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "     POLL TIMEOUT after ${POLL_MAX_SECONDS}s -- DO API may be slow; verify manually" >&2
    fail_poll=1
  fi
done

if [ "$fail_destroy" = "1" ]; then exit 2; fi
if [ "$fail_poll" = "1" ]; then exit 3; fi
echo "  done -- $count droplet(s) destroyed"
