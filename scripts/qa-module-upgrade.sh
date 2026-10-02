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
# PROVENANCE (GOL-2454): QA floats `main` via git-sync, so "I ran the upgrade"
# never told us WHICH commit was upgraded — and an `-u` against a stale or
# mid-pull checkout silently gates the wrong code. This script now prints the
# synced commit + each module's on-disk manifest version BEFORE upgrading, and
# `EXPECT_REF=<40-char sha>` makes it fail CLOSED when the droplet is not on
# the commit you meant to gate. Always pass EXPECT_REF for a release-train
# `-u` — that pass is what a money-path promote is gated on.
#
# Usage:
#   scripts/qa-module-upgrade.sh <module> [<module> ...]
#   QA_HOST=root@odoo.qa.gatheringatthegrove.com scripts/qa-module-upgrade.sh grove_headless
#   EXPECT_REF=cf519bfa8ee59c493fc94eee15d77a504e7a09a7 \
#     scripts/qa-module-upgrade.sh grove_headless
###############################################################################
set -euo pipefail

QA_HOST="${QA_HOST:-root@odoo.qa.gatheringatthegrove.com}"
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
EXPECT_REF="${EXPECT_REF:-}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <module> [<module> ...]" >&2
  echo "       EXPECT_REF=<40-char sha> $0 <module>   # fail closed on a stale checkout" >&2
  exit 2
fi

# Validate EXPECT_REF locally so a typo fails before we touch the droplet.
if [ -n "${EXPECT_REF}" ] && ! printf '%s' "${EXPECT_REF}" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "ERROR: EXPECT_REF must be a full 40-char lowercase hex commit SHA (got: ${EXPECT_REF})" >&2
  exit 2
fi

# Comma-join the module list for Odoo's -u flag.
MODULES="$(printf '%s,' "$@")"
MODULES="${MODULES%,}"

echo ">> QA module upgrade: -u ${MODULES} on ${QA_HOST} (${DEPLOY_DIR})"
if [ -n "${EXPECT_REF}" ]; then
  echo ">> expecting git-sync checkout at ${EXPECT_REF} (fail-closed)"
else
  echo ">> WARNING: no EXPECT_REF set — upgrading whatever git-sync last pulled."
  echo ">>          For a release-train -u, re-run with EXPECT_REF=<sha>."
fi

# WARNING (GOL-2531): everything between the opening and closing double quote
# below is expanded by THIS shell before ssh runs -- comments included. An
# unescaped backtick or $ in prose executes/interpolates LOCALLY: a comment
# reading "inspect it with `printenv`" once spliced the whole local environment
# (a 1Password service-account token among it) into this payload and forced a
# rotation. Escape prose as \` and \$; write remote substitutions as \$( ... ).
# Never print the rendered payload from a shell that holds secrets -- run
# `python3 scripts/check-ssh-payload-escaping.py` (CI: "ssh payload render
# guard") or `bash -n` instead.
# shellcheck disable=SC2029  # we WANT MODULES/DEPLOY_DIR/EXPECT_REF expanded locally.
ssh -o StrictHostKeyChecking=yes "${QA_HOST}" "
  set -euo pipefail
  cd '${DEPLOY_DIR}'
  set -a; . ./.env; set +a
  dc() { docker compose --env-file '${DEPLOY_DIR}/.env' \"\$@\"; }

  echo '>> resolving git-sync checkout provenance'
  # The git-sync sidecar owns the clone and ships git; ask it first. Fall back
  # to the symlink target basename (git-sync names each worktree by commit) as
  # read from the odoo container, which mounts the same volume.
  SYNCED=\"\$(dc exec -T custom-modules-sync git -C /workspace/current rev-parse HEAD 2>/dev/null | tr -d '\r\n' || true)\"
  if ! printf '%s' \"\$SYNCED\" | grep -Eq '^[0-9a-f]{40}\$'; then
    SYNCED=\"\$(dc exec -T odoo sh -c 'basename \"\$(readlink -f /workspace/current)\"' 2>/dev/null | tr -d '\r\n' || true)\"
  fi
  if printf '%s' \"\$SYNCED\" | grep -Eq '^[0-9a-f]{40}\$'; then
    echo \">> git-sync /workspace/current = \$SYNCED\"
  else
    echo '>> git-sync commit: UNRESOLVED (neither sidecar git nor symlink target gave a SHA)'
    SYNCED=''
  fi

  if [ -n '${EXPECT_REF}' ]; then
    if [ -z \"\$SYNCED\" ]; then
      echo 'ERROR: EXPECT_REF was set but the synced commit could not be resolved — refusing to upgrade blind.' >&2
      exit 3
    fi
    if [ \"\$SYNCED\" != '${EXPECT_REF}' ]; then
      echo \"ERROR: git-sync is on \$SYNCED but EXPECT_REF=${EXPECT_REF}.\" >&2
      echo '       QA has not pulled the commit you meant to gate (or is mid-pull).' >&2
      echo '       Wait a sync period and re-run, or fix CUSTOM_MODULES_REF in ${DEPLOY_DIR}/.env.' >&2
      exit 3
    fi
    echo '>> checkout matches EXPECT_REF'
  fi

  echo '>> on-disk manifest versions (pre-upgrade)'
  for mod in \$(printf '%s' '${MODULES}' | tr ',' ' '); do
    # Keep digits and dots only, so the manifest line
    #     \"version\": \"19.0.1.51.0\",
    # yields 19.0.1.51.0 without nested quoting the ssh layer would mangle.
    ver=\"\$(dc exec -T odoo grep -m1 version \"/workspace/current/\$mod/__manifest__.py\" 2>/dev/null | cut -d: -f2 | tr -cd '0-9.' || true)\"
    echo \"   \$mod: \${ver:-<unreadable>}\"
  done

  echo '>> running -u ${MODULES} --stop-after-init (server down for the init window)'
  dc exec -T odoo odoo -d \"\$DB_NAME\" -u '${MODULES}' --stop-after-init --no-http --workers=0
  echo '>> restarting long-running odoo server'
  dc restart odoo
  echo '>> QA upgrade of ${MODULES} complete at commit '\"\${SYNCED:-<unresolved>}\"'.'
  echo '>> verify installed_version over XML-RPC (docs/RUNBOOK-qa-module-upgrade.md § Verify).'
"
