#!/usr/bin/env bash
# GOL-53 P0.4b — Create DigitalOcean Monitoring alert policies for the AgenticOS droplet
# and route them to the ops Discord channel.
#
# Creates three policies on droplet 572389418:
#   - memory  > 80%  / 5m   (warning)
#   - memory  > 90%  / 5m   (critical)
#   - disk    > 85%  / 5m   (warning)
# (Load / CPU can be added later; memory is the incident driver.)
#
# Discord routing: DO alert policies natively support email + Slack, not Discord.
# Discord's incoming webhooks are Slack-compatible when you append `/slack` to the
# webhook URL, so we register the Discord webhook (with /slack suffix) as a Slack
# channel. This is the standard, supported bridge — no extra infra.
#
# Idempotent: looks up existing policies by a GOL-53 tag in the description and skips
# re-creating them. Re-run safely; use --replace to delete+recreate (e.g. after tuning).
#
# Requires (run from ANY env with network + a DO token — does NOT need SSH to the host):
#   DO_API_TOKEN            write-scoped: monitoring:read+write   (REQUIRED)
#   OPS_DISCORD_WEBHOOK     Discord webhook URL for the ops/alerts channel (REQUIRED)
#   DROPLET_ID              default 572389418
#   DISCORD_CHANNEL         label shown in DO, default "grove-ops"
#
# Usage:
#   DO_API_TOKEN=... OPS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy \
#     bash 04-do-alert-policies.sh
set -euo pipefail

: "${DO_API_TOKEN:?set DO_API_TOKEN (monitoring:read+write)}"
: "${OPS_DISCORD_WEBHOOK:?set OPS_DISCORD_WEBHOOK (Discord ops webhook URL)}"
DROPLET_ID="${DROPLET_ID:-572389418}"
DISCORD_CHANNEL="${DISCORD_CHANNEL:-grove-ops}"
REPLACE="${1:-}"

API="https://api.digitalocean.com/v2/monitoring/alerts"
AUTH=(-H "Authorization: Bearer ${DO_API_TOKEN}" -H "Content-Type: application/json")

# Discord accepts Slack-formatted payloads at <webhook>/slack.
SLACK_URL="${OPS_DISCORD_WEBHOOK%/}"
[[ "${SLACK_URL}" == */slack ]] || SLACK_URL="${SLACK_URL}/slack"

TAG="[GOL-53]"

# ---- helpers -------------------------------------------------------------------
list_policies() { curl -sS "${AUTH[@]}" "${API}?per_page=200"; }

policy_exists() { # $1 = description substring
  list_policies | python3 -c "import sys,json;d=json.load(sys.stdin);import re
sub=sys.argv[1]
print('yes' if any(sub in (p.get('description') or '') for p in d.get('policies',[])) else 'no')" "$1"
}

delete_tagged() {
  list_policies | python3 -c "import sys,json
d=json.load(sys.stdin)
for p in d.get('policies',[]):
  if '$TAG' in (p.get('description') or ''): print(p['uuid'])" | while read -r uuid; do
    [[ -n "$uuid" ]] && echo "    deleting existing ${uuid}" && curl -sS -X DELETE "${AUTH[@]}" "${API}/${uuid}" >/dev/null
  done
}

create_policy() { # $1 type  $2 compare  $3 value  $4 window  $5 desc
  local type="$1" compare="$2" value="$3" window="$4" desc="$5"
  if [[ "${REPLACE}" != "--replace" ]] && [[ "$(policy_exists "${desc}")" == "yes" ]]; then
    echo "    exists, skipping: ${desc}"
    return 0
  fi
  python3 - "$type" "$compare" "$value" "$window" "$desc" "$DROPLET_ID" "$DISCORD_CHANNEL" "$SLACK_URL" <<'PY' | curl -sS -X POST "${AUTH[@]}" "${API}" -d @- | python3 -c "import sys,json;d=json.load(sys.stdin);p=d.get('policy',{});print('    created',p.get('uuid'),'—',p.get('description') or d)"
import sys, json
type_, compare, value, window, desc, droplet, channel, slack = sys.argv[1:9]
print(json.dumps({
  "alerts": {"slack": [{"channel": channel, "url": slack}], "email": []},
  "compare": compare,
  "description": desc,
  "enabled": True,
  "entities": [droplet],
  "tags": [],
  "type": type_,
  "value": float(value),
  "window": window,
}))
PY
}

echo "==> AgenticOS DO alert policies (droplet ${DROPLET_ID}) -> Discord '${DISCORD_CHANNEL}'"
[[ "${REPLACE}" == "--replace" ]] && { echo "    --replace: removing existing ${TAG} policies"; delete_tagged; }

# DO metric type slugs: v1/insights/droplet/<metric>
create_policy "v1/insights/droplet/memory_utilization_percent" "GreaterThan" 80 "5m" "${TAG} AgenticOS memory > 80% (warning)"
create_policy "v1/insights/droplet/memory_utilization_percent" "GreaterThan" 90 "5m" "${TAG} AgenticOS memory > 90% (critical)"
create_policy "v1/insights/droplet/disk_utilization_percent"   "GreaterThan" 85 "5m" "${TAG} AgenticOS disk > 85% (warning)"

echo "==> Current ${TAG} policies:"
list_policies | python3 -c "import sys,json
for p in json.load(sys.stdin).get('policies',[]):
  if '$TAG' in (p.get('description') or ''):
    print('   -',p['uuid'], p['type'].split('/')[-1], p['compare'], p['value'], p['window'], '::', p['description'])"

echo "==> Done. Fire a test from the DO panel or wait for the next threshold breach."
echo "    Rollback: bash 04-do-alert-policies.sh --replace  (then Ctrl-C), or delete by uuid:"
echo "      curl -X DELETE -H 'Authorization: Bearer \$DO_API_TOKEN' ${API}/<uuid>"
