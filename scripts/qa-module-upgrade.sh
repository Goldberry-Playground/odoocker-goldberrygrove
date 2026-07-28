#!/usr/bin/env bash
###############################################################################
# qa-module-upgrade.sh — run an explicit `-u <module>` migration/upgrade pass
# on the L3 QA Odoo droplet.
#
# WHY: the QA git-sync sidecar delivers grove-odoo-modules CODE, but the `qa`
# entrypoint branch boots with --init=base and NO --update, so a restart (or a
# droplet replace) never runs a module's new migrations / registers new fields.
# Only an explicit `-u <module>` does. See docs/RUNBOOK-qa-module-upgrade.md.
#
# ACCESS: port 22 on the L3 droplet is firewalled to the admin IP. Run this
# from an operator machine that holds the admin IP + the grove-qa SSH key.
# It is intentionally NOT a CI job (GitHub runners are not on the admin IP).
#
# Idempotent: `-u` re-applies module state and skips migrations already
# recorded in ir_module_module, so re-running is safe.
#
# Usage:
#   scripts/qa-module-upgrade.sh <module> [<module> ...]
#   QA_HOST=root@odoo.qa.gatheringatthegrove.com scripts/qa-module-upgrade.sh grove_headless
###############################################################################
set -euo pipefail

QA_HOST="${QA_HOST:-root@odoo.qa.gatheringatthegrove.com}"
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <module> [<module> ...]" >&2
  exit 2
fi

# Comma-join the module list for Odoo's -u flag.
MODULES="$(printf '%s,' "$@")"
MODULES="${MODULES%,}"

echo ">> QA module upgrade: -u ${MODULES} on ${QA_HOST} (${DEPLOY_DIR})"

# shellcheck disable=SC2029  # we WANT MODULES/DEPLOY_DIR expanded locally.
ssh -o StrictHostKeyChecking=yes "${QA_HOST}" "
  set -euo pipefail
  cd '${DEPLOY_DIR}'
  set -a; . ./.env; set +a
  echo '>> running -u ${MODULES} --stop-after-init (server down for the init window)'
  docker compose --env-file '${DEPLOY_DIR}/.env' exec -T odoo \
    odoo -d \"\$DB_NAME\" -u '${MODULES}' --stop-after-init --no-http --workers=0
  echo '>> restarting long-running odoo server'
  docker compose --env-file '${DEPLOY_DIR}/.env' restart odoo
  echo '>> QA upgrade of ${MODULES} complete.'
"
