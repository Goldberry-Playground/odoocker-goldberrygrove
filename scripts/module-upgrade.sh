#!/usr/bin/env bash
###############################################################################
# module-upgrade.sh — force the entrypoint's built-in custom-module upgrade to
# re-run on an L3 Odoo droplet (QA or production).
#
# THE DURABLE PATH IS AUTOMATIC. odoo/entrypoint.sh runs a blocking
# `--init=base,<mods> --update=<mods>` at boot whenever git-sync advances the
# modules revision (AUTO_UPGRADE_MODULES, set in the qa + production compose).
# A new model's DB table is therefore created on every deploy that advances the
# revision — no manual step. See docs/RUNBOOK-module-upgrade.md (GOL-1009).
#
# THIS SCRIPT IS THE ESCAPE HATCH. It clears the revision marker and restarts
# odoo, so the entrypoint re-runs the upgrade of AUTO_UPGRADE_MODULES on the
# CURRENT revision (without a redeploy / droplet replace). Reach for it to:
#   - re-drive a migration after a hand-edit on the droplet, or
#   - bootstrap the marker/tables on a droplet that predates the entrypoint fix.
# It reuses the SAME tested code path, so it cannot get odoo.conf generation
# wrong the way an ad-hoc `docker run odoo -u ...` would.
#
# For a module NOT in AUTO_UPGRADE_MODULES: add it to AUTO_UPGRADE_MODULES in
# the droplet's /etc/grove compose env and run this script (see the runbook).
#
# ACCESS: port 22 on the L3 droplet is firewalled to the admin IP. Run from an
# operator machine holding the admin IP + the droplet's SSH key. Intentionally
# NOT a CI job (GitHub runners are not on the admin IP).
#
# The marker lives on the durable filestore bind-mount (host /mnt/odoo-filestore
# -> container /var/lib/odoo), so this works even when odoo is crash-looping.
#
# Usage:
#   scripts/module-upgrade.sh
#   QA_HOST=root@odoo.qa.gatheringatthegrove.com scripts/module-upgrade.sh
#   QA_HOST=root@<prod-odoo-host> DEPLOY_DIR=/etc/grove scripts/module-upgrade.sh
###############################################################################
set -euo pipefail

QA_HOST="${QA_HOST:-root@odoo.qa.gatheringatthegrove.com}"
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
FILESTORE_DIR="${FILESTORE_DIR:-/mnt/odoo-filestore}"
MARKER="${MARKER:-${FILESTORE_DIR}/.grove-modules-rev}"

echo ">> forcing module-upgrade re-run on ${QA_HOST} (clear ${MARKER} + restart odoo)"

# shellcheck disable=SC2029  # we WANT the paths expanded locally.
ssh -o StrictHostKeyChecking=yes "${QA_HOST}" "
  set -euo pipefail
  cd '${DEPLOY_DIR}'
  echo '>> clearing revision marker ${MARKER}'
  rm -f '${MARKER}'
  echo '>> restarting odoo (entrypoint re-runs -u AUTO_UPGRADE_MODULES on the current revision)'
  docker compose --env-file '${DEPLOY_DIR}/.env' restart odoo
  echo '>> tailing odoo boot log for the GOL-1009 upgrade lines'
  docker compose --env-file '${DEPLOY_DIR}/.env' logs --tail=60 odoo | grep -E 'GOL-1009|GOL-746|Modules loaded|Registry' || true
  echo '>> done. If you see \"module upgrade complete\", the new tables now exist.'
"
