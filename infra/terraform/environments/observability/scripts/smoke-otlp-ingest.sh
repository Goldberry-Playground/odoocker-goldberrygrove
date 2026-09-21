#!/usr/bin/env bash
# Smoke-test the public OTLP ingest endpoint (GOL-2330).
#
# Proves the success condition end to end from an off-droplet vantage (a
# laptop or a GitHub-Actions runner):
#   1. no Bearer         -> 403 (Cloudflare WAF edge block)
#   2. wrong Bearer      -> 403 (Cloudflare WAF edge block)
#   3. correct Bearer    -> 2xx from OpenObserve, and a gauge named
#      grove_otlp_ingest_smoke lands in the `default` org metrics.
# A 403 whose body is "grove-obs: forbidden" came from the ORIGIN (Caddy), not
# the edge -- i.e. the WAF rule is not active. The edge 403 is Cloudflare's HTML
# block page (cf-ray header present, no "grove-obs" body).
#
# Usage: OTLP_INGEST_BEARER=<token> ./scripts/smoke-otlp-ingest.sh [host]
set -euo pipefail

HOST="${1:-otlp-ingest.gatheringatthegrove.com}"
TOKEN="${OTLP_INGEST_BEARER:?set OTLP_INGEST_BEARER (1P Grove Infra/otlp_ingest_bearer_token)}"
URL="https://${HOST}/api/default/v1/metrics"
NOW_NS="$(date +%s)000000000"
BODY=$(cat <<JSON
{"resourceMetrics":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"otlp-ingest-smoke"}}]},
 "scopeMetrics":[{"metrics":[{"name":"grove_otlp_ingest_smoke","gauge":{"dataPoints":[{"asDouble":1,"timeUnixNano":"${NOW_NS}"}]}}]}]}]}
JSON
)

fail=0
check() { # label expected-regex curl-args...
  local label="$1" want="$2"; shift 2
  local code
  code=$(curl -sS -o /tmp/otlp-smoke.out -w '%{http_code}' -X POST "$URL" \
    -H 'Content-Type: application/json' --data "$BODY" "$@" || true)
  if [[ "$code" =~ $want ]]; then
    echo "PASS  ${label}: HTTP ${code}"
  else
    echo "FAIL  ${label}: HTTP ${code} (want ${want})"; head -c 300 /tmp/otlp-smoke.out; echo
    fail=1
  fi
  if [ "$code" = "403" ] && grep -q 'grove-obs: forbidden' /tmp/otlp-smoke.out; then
    echo "WARN  ${label}: 403 came from the ORIGIN, not the CF edge -- WAF Bearer rule not active"
    fail=1
  fi
}

check "no bearer"      '^403$'
check "wrong bearer"   '^403$' -H "Authorization: Bearer wrong-${RANDOM}"
check "correct bearer" '^2[0-9][0-9]$' -H "Authorization: Bearer ${TOKEN}"

[ "$fail" = 0 ] && echo "OK: edge rejects unauthenticated, authorized metric accepted (look for grove_otlp_ingest_smoke in OpenObserve)."
exit "$fail"
