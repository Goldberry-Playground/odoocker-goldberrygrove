#!/usr/bin/env bash
# GOL-53 P0.3 — Right-size the AgenticOS droplet one tier up. ⚠️ BOARD-APPROVAL GATED.
#
# Why: after guardrails (swap + mem_limits), give the box real headroom so normal busy
# load doesn't sit at 100%. CPU/RAM resize is REVERSIBLE; we pass --resize-disk=false so
# the disk is NOT grown (disk resize is one-way/permanent). Requires a brief power-off →
# resize → power-on (~1-2 min downtime). Est. +$6-24/mo depending on the tier jump.
#
# DO NOT RUN without recorded board approval (spend + brief downtime). Est. cost and the
# approval reference are required as env vars so this can't be run casually.
#
# Requires (run from ANY env with a DO token — does NOT need SSH to the host):
#   DO_API_TOKEN     write-scoped: droplet:read+write   (REQUIRED)
#   TARGET_SIZE      target slug, e.g. s-2vcpu-4gb       (REQUIRED)
#   APPROVAL_REF     board approval id/URL for the audit trail (REQUIRED)
#   DROPLET_ID       default 572389418
#
# Usage:
#   DO_API_TOKEN=... TARGET_SIZE=s-2vcpu-4gb APPROVAL_REF=GOL-53-approval-xxxx \
#     bash 05-resize.sh
set -euo pipefail

: "${DO_API_TOKEN:?set DO_API_TOKEN (droplet:read+write)}"
: "${TARGET_SIZE:?set TARGET_SIZE (e.g. s-2vcpu-4gb)}"
: "${APPROVAL_REF:?set APPROVAL_REF (board approval id/URL) — resize is spend-gated}"
DROPLET_ID="${DROPLET_ID:-572389418}"

API="https://api.digitalocean.com/v2"
AUTH=(-H "Authorization: Bearer ${DO_API_TOKEN}" -H "Content-Type: application/json")

echo "==> Current droplet ${DROPLET_ID}:"
curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}" | python3 -c "import sys,json
d=json.load(sys.stdin)['droplet']
print('   name:',d['name'],'| size:',d['size_slug'],'| vcpus:',d['vcpus'],'| mem:',d['memory'],'MB | status:',d['status'])"

echo
echo "==> Requested resize: -> ${TARGET_SIZE}  (disk NOT resized; reversible)"
echo "    Approval ref: ${APPROVAL_REF}"
read -r -p "    Type the droplet name to confirm brief-downtime resize: " CONFIRM
NAME=$(curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}" | python3 -c "import sys,json;print(json.load(sys.stdin)['droplet']['name'])")
if [[ "${CONFIRM}" != "${NAME}" ]]; then
  echo "    name mismatch ('${CONFIRM}' != '${NAME}'); aborting." >&2
  exit 1
fi

# DO requires the droplet powered off for a resize. Use the droplet-action resize with
# disk:false; --wait equivalent = poll the action to completion.
echo "==> Powering off (graceful)."
curl -sS -X POST "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}/actions" -d '{"type":"shutdown"}' >/dev/null || true
# Wait up to ~90s for off, then hard power_off if still on.
for i in $(seq 1 18); do
  ST=$(curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}" | python3 -c "import sys,json;print(json.load(sys.stdin)['droplet']['status'])")
  [[ "${ST}" == "off" ]] && break
  sleep 5
done
ST=$(curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}" | python3 -c "import sys,json;print(json.load(sys.stdin)['droplet']['status'])")
if [[ "${ST}" != "off" ]]; then
  echo "    graceful shutdown timed out; issuing power_off."
  curl -sS -X POST "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}/actions" -d '{"type":"power_off"}' >/dev/null
  sleep 10
fi

echo "==> Resizing to ${TARGET_SIZE} (disk:false)."
ACTION=$(curl -sS -X POST "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}/actions" \
  -d "{\"type\":\"resize\",\"disk\":false,\"size\":\"${TARGET_SIZE}\"}")
AID=$(echo "${ACTION}" | python3 -c "import sys,json;print(json.load(sys.stdin)['action']['id'])")
echo "    resize action ${AID}; waiting for completion..."
for i in $(seq 1 60); do
  AST=$(curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}/actions/${AID}" | python3 -c "import sys,json;print(json.load(sys.stdin)['action']['status'])")
  [[ "${AST}" == "completed" ]] && { echo "    resize completed."; break; }
  [[ "${AST}" == "errored" ]] && { echo "    resize ERRORED." >&2; exit 1; }
  sleep 5
done

echo "==> Powering back on."
curl -sS -X POST "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}/actions" -d '{"type":"power_on"}' >/dev/null

echo "==> New state:"
sleep 8
curl -sS "${AUTH[@]}" "${API}/droplets/${DROPLET_ID}" | python3 -c "import sys,json
d=json.load(sys.stdin)['droplet']
print('   size:',d['size_slug'],'| vcpus:',d['vcpus'],'| mem:',d['memory'],'MB | status:',d['status'])"

echo "==> Done. Verify the control plane came up (Healthchecks ping + :1933 UI)."
echo "    ROLLBACK (reversible): re-run with TARGET_SIZE=<previous slug>."
