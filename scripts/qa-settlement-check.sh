#!/usr/bin/env bash
###############################################################################
# qa-settlement-check.sh — prove the ship-time balance charge on QA against
# real Stripe TEST mode (release-train money-path check).
#
# WHY: the e2e gate's checkout-deposit-happy-path spec pays the flat $10
# deposit and saves the card, but nothing then SHIPS that order, so the
# off-session balance capture (GOL-2053, settle_order_at_ship) — the bulk of
# every pre-order's money — was only ever unit-tested with Stripe mocked.
# This takes one deposit_paid order the gate just created, simulates the label
# purchase (no real label is bought), runs the same _operator_mark_shipped the
# Discord "Mark Shipped" button runs, and asserts the balance was captured.
#
# WHEN: release-train week, AFTER qa-module-upgrade.sh and a green e2e gate
# (the gate is what creates the deposit order), BEFORE the Wed promote.
#
# SAFETY:
#   - DRY RUN BY DEFAULT: reports the order it would ship. APPLY=1 ships it and
#     charges the balance (Stripe test mode, so no real money).
#   - The payload resolves the exact Stripe key settlement will use and FAILS
#     CLOSED unless it is an sk_test_/rk_test_ key.
#   - QA only: the target is the L3 QA droplet; there is no prod switch.
#   - Side effect besides the charge: the order's customer gets the branded
#     "shipped" email (the gate's checkout address).
#
# ACCESS: same as qa-module-upgrade.sh — port 22 is firewalled to the admin
# IP, so run it from an operator machine with the grove-qa SSH key. Not CI.
#
# The Python payload travels on STDIN (scripts/qa-settlement-check.py), so no
# prose or code in it is ever expanded by this shell (GOL-2531).
#
# Usage:
#   scripts/qa-settlement-check.sh                 # dry run, newest eligible order
#   APPLY=1 scripts/qa-settlement-check.sh         # ship it + charge the balance
#   ORDER=S00412 APPLY=1 scripts/qa-settlement-check.sh
#   SHIP_COST=14.75 APPLY=1 scripts/qa-settlement-check.sh
###############################################################################
set -euo pipefail

QA_HOST="${QA_HOST:-root@odoo.qa.gatheringatthegrove.com}"
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
ORDER="${ORDER:-}"
SHIP_COST="${SHIP_COST:-9.00}"
APPLY="${APPLY:-0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="$HERE/qa-settlement-check.py"

# Validate everything spliced into the remote command BEFORE touching the droplet.
if [ -n "$ORDER" ] && ! printf '%s' "$ORDER" | grep -Eq '^S[0-9]{4,6}$'; then
  echo "ERROR: ORDER must look like S00412 (got: $ORDER)" >&2
  exit 2
fi
if ! printf '%s' "$SHIP_COST" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,2})?$'; then
  echo "ERROR: SHIP_COST must be a dollar amount like 9.00 (got: $SHIP_COST)" >&2
  exit 2
fi
case "$APPLY" in 0 | 1) ;; *)
  echo "ERROR: APPLY must be 0 or 1 (got: $APPLY)" >&2
  exit 2
  ;;
esac
case "$DEPLOY_DIR" in /*) ;; *)
  echo "ERROR: DEPLOY_DIR must be an absolute path (got: $DEPLOY_DIR)" >&2
  exit 2
  ;;
esac
[ -f "$PAYLOAD" ] || { echo "ERROR: payload not found: $PAYLOAD" >&2; exit 2; }

if [ "$APPLY" = "1" ]; then
  echo ">> QA settlement check on ${QA_HOST}: APPLY — will ship ${ORDER:-the newest eligible order} and charge its balance (Stripe TEST)"
else
  echo ">> QA settlement check on ${QA_HOST}: DRY RUN (APPLY=1 to ship + charge)"
fi

out="$(mktemp)"
trap 'rm -f "$out"' EXIT

# Only validated values are expanded locally; \$DB_NAME expands on the droplet.
# shellcheck disable=SC2029  # we WANT DEPLOY_DIR/ORDER/SHIP_COST/APPLY expanded locally.
ssh -o StrictHostKeyChecking=yes "${QA_HOST}" "
  set -euo pipefail
  cd '${DEPLOY_DIR}'
  set -a; . ./.env; set +a
  docker compose --env-file '${DEPLOY_DIR}/.env' exec -T \
    -e QA_SETTLE_ORDER='${ORDER}' -e QA_SETTLE_SHIP_COST='${SHIP_COST}' -e QA_SETTLE_APPLY='${APPLY}' \
    odoo odoo shell -d \"\$DB_NAME\" --no-http --log-level=warn
" <"$PAYLOAD" | tee "$out"

result="$(grep -E '^RESULT: ' "$out" | tail -1 || true)"
case "$result" in
  "RESULT: PASS"*) exit 0 ;;
  "RESULT: DRYRUN"*) exit 0 ;;
  "RESULT: FAIL"*) exit 1 ;;
  *)
    echo "ERROR: payload printed no RESULT line (odoo shell failed to start?)" >&2
    exit 1
    ;;
esac
