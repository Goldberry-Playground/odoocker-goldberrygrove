#!/usr/bin/env bash
###############################################################################
# prod-modules-promote.sh -- promote the prod grove-odoo-modules pin
# (`CUSTOM_MODULES_REF`) to a reviewed SHA, run the migrations, and PROVE the
# money-path migration actually bound every company.
#
# WHY THIS EXISTS (GOL-2346, release Train #1)
# ---------------------------------------------------------------------------
# A Grove promote is TWO legs, not one workflow:
#
#   Leg A -- storefronts: fully automated by
#            .github/workflows/promote-storefronts.yml (bump image tags ->
#            targeted apply -> doctl create-deployment -> verify -> Discord).
#            That workflow bumps `hub_image_tag` + `tenant_image_tag` ONLY and
#            HARD-ABORTS if the sed ever touches `custom_modules_ref`
#            (GOL-1708 finding 1) -- so it can never do Leg B.
#
#   Leg B -- modules (THIS SCRIPT): `custom_modules_ref` feeds
#            cloud-init-odoo.yaml.tpl -> `user_data`, and
#            `digitalocean_droplet.odoo` carries
#            `lifecycle { ignore_changes = [user_data, ...] }`. So a plain
#            `terraform apply` of a new pin is a NO-OP, and forcing it through
#            means a droplet REPLACE (outage + the GOL-93 filestore gate).
#            The established safe path (precedents 34cc4548 / 22fbb71 /
#            0b36ecfa) is: edit CUSTOM_MODULES_REF in the droplet's
#            /etc/grove/.env -> git-sync pulls -> the entrypoint's
#            AUTO_UPGRADE_MODULES pass runs the migrations -> THEN converge the
#            committed Terraform default onto what is now live
#            (`scripts/reconcile_modules_pin.py`, rollback-safe; merge != deploy).
#
# Leg B was the only part of a promote with no tooling: a human sed on a live
# revenue box, with the success condition held in someone's head. This script
# is that path, made idempotent, fail-closed, and self-verifying.
#
# THE MONEY GUARD (the reason this is not just a convenience wrapper)
# ---------------------------------------------------------------------------
# grove_headless's `setup_wv_sales_tax` (hooks.py) wraps EVERY company in
# try/except and swallows failures at WARNING so tax setup can never abort an
# upgrade. That is the right availability call -- but it means a PARTIAL BIND
# EXITS 0 AND LOOKS LIKE SUCCESS. A company left on the old 7% / demo 15% tax
# mis-charges every order it takes. "The upgrade finished" is therefore NOT the
# success condition. The real one is this line in the odoo log:
#
#     grove_headless: WV 6% state sales tax bound for N of N companies
#
# where both numbers match. This script parses that line and exits non-zero on
# `bound for only X of N` (or on the line being absent when grove_headless was
# in the upgrade set). See docs/RUNBOOK-module-upgrade.md "Promoting the prod
# modules pin (Leg B)".
#
# ACCESS: port 22 on the prod droplet is firewalled to the admin IP. Run from
# an operator machine holding the admin IP + the droplet's SSH key.
# Intentionally NOT a CI job (GitHub runners are not on the admin IP, and a
# prod module migration should never fire from a push).
#
# SAFE BY DEFAULT: with no CONFIRM=PROMOTE, this runs the read-only pre-flight
# and stops. Nothing on the droplet is written. Run it that way first, always.
#
# IDEMPOTENT: re-running against a droplet already on TARGET_REF re-reports
# state and makes no change (the entrypoint's revision marker turns the restart
# into a documented no-op). A resumed/retried run converges; it does not
# accrete.
#
# Usage:
#   TARGET_REF=<40-char sha> scripts/prod-modules-promote.sh            # pre-flight only
#   TARGET_REF=<40-char sha> CONFIRM=PROMOTE scripts/prod-modules-promote.sh
#   PROD_HOST=root@<prod-odoo-host> TARGET_REF=<sha> CONFIRM=PROMOTE ...
#
# After a successful run, converge committed HCL onto the now-live pin:
#   gh workflow run reconcile-modules-pin.yml -f modules_sha=<TARGET_REF>
###############################################################################
set -euo pipefail

PROD_HOST="${PROD_HOST:-root@odoo.gatheringatthegrove.com}"
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
FILESTORE_DIR="${FILESTORE_DIR:-/mnt/odoo-filestore}"
MARKER="${MARKER:-${FILESTORE_DIR}/.grove-modules-rev}"
TARGET_REF="${TARGET_REF:-}"
CONFIRM="${CONFIRM:-}"
# git-sync's GITSYNC_PERIOD defaults to 60s; allow a few periods plus clone time.
SYNC_TIMEOUT="${SYNC_TIMEOUT:-300}"
# The blocking --init/--update pass on a real DB can take a few minutes.
UPGRADE_TIMEOUT="${UPGRADE_TIMEOUT:-900}"
# Seconds between polls of the two waits above. Exposed only so the test
# harness can run them fast; leave it alone in real use.
POLL_INTERVAL="${POLL_INTERVAL:-10}"

die() { echo "ERROR: $*" >&2; exit 2; }

[ -n "${TARGET_REF}" ] || die "TARGET_REF is required (the reviewed grove-odoo-modules SHA to promote).
       usage: TARGET_REF=<40-char sha> [CONFIRM=PROMOTE] $0"

# Validate locally so a typo fails before we ever reach the droplet. This is
# the same 40-char-hex contract var.custom_modules_ref's own validation block
# enforces (GOL-892) -- a branch name here would make compose fail at `up`.
printf '%s' "${TARGET_REF}" | grep -Eq '^[0-9a-f]{40}$' \
  || die "TARGET_REF must be a full 40-char lowercase hex commit SHA (got: ${TARGET_REF})"

if [ -n "${CONFIRM}" ] && [ "${CONFIRM}" != "PROMOTE" ]; then
  die "CONFIRM must be exactly PROMOTE to mutate production (got: ${CONFIRM})"
fi

# Explicit if/then rather than `A && B` (repo shell audit 2026-06-29): under
# `set -e` an AND-list whose left side is false is exempt from errexit, but the
# explicit form is the one this repo reads consistently.
MODE="preflight"
if [ "${CONFIRM}" = "PROMOTE" ]; then
  MODE="promote"
fi

echo "== prod modules promote (${MODE}) =="
echo "   host:       ${PROD_HOST}"
echo "   deploy dir: ${DEPLOY_DIR}"
echo "   target ref: ${TARGET_REF}"
if [ "${MODE}" = "preflight" ]; then
  echo "   NOTE: read-only pre-flight. Nothing will be written."
  echo "         Re-run with CONFIRM=PROMOTE to execute."
fi
echo

# shellcheck disable=SC2029  # we WANT the local vars expanded here, not on the droplet.
ssh -o StrictHostKeyChecking=yes "${PROD_HOST}" "
  set -euo pipefail
  cd '${DEPLOY_DIR}'
  dc() { docker compose --env-file '${DEPLOY_DIR}/.env' \"\$@\"; }

  TARGET='${TARGET_REF}'
  MODE='${MODE}'

  # --- pre-flight 1: the committed pin currently in the droplet's env file ---
  CURRENT=\"\$(sed -n 's/^CUSTOM_MODULES_REF=//p' '${DEPLOY_DIR}/.env' | tail -1 | tr -d '\r\"' )\"
  echo \">> /etc/grove/.env CUSTOM_MODULES_REF = \${CURRENT:-<unset>}\"

  # --- pre-flight 2: what git-sync has ACTUALLY checked out right now --------
  # git-sync v4 names each worktree by commit and repoints /workspace/current
  # at it, so the symlink target's basename IS the live revision. Ask the
  # sidecar's git first; fall back to the symlink as read from odoo (same
  # volume, mounted read-only there).
  SYNCED=\"\$(dc exec -T custom-modules-sync git -C /workspace/current rev-parse HEAD 2>/dev/null | tr -d '\r\n' || true)\"
  if ! printf '%s' \"\$SYNCED\" | grep -Eq '^[0-9a-f]{40}\$'; then
    SYNCED=\"\$(dc exec -T odoo sh -c 'basename \"\$(readlink -f /workspace/current)\"' 2>/dev/null | tr -d '\r\n' || true)\"
  fi
  printf '%s' \"\$SYNCED\" | grep -Eq '^[0-9a-f]{40}\$' || SYNCED=''
  echo \">> git-sync /workspace/current  = \${SYNCED:-<unresolved>}\"

  # --- pre-flight 3: the entrypoint's last-upgraded revision marker ---------
  LAST=\"\$(cat '${MARKER}' 2>/dev/null | tr -d '\r\n' || true)\"
  echo \">> upgrade marker ${MARKER} = \${LAST:-<none>}\"

  # --- pre-flight 4: the deployed compose MUST declare AUTO_UPGRADE_MODULES -
  # This is the whole migration mechanism. The deployed
  # /etc/grove/docker-compose.yml is written from cloud-init user_data, which
  # is in the droplet's terraform ignore_changes -- so a box provisioned before
  # AUTO_UPGRADE_MODULES was added to the source compose will NOT have it, and
  # the restart below would silently skip every migration and serve a
  # half-migrated DB. Fail closed rather than discover that from a wrong tax.
  AUTO=\"\$(grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' '${DEPLOY_DIR}/docker-compose.yml' 2>/dev/null | head -1 | cut -d: -f2- | tr -d ' \r' || true)\"
  if [ -z \"\$AUTO\" ]; then
    echo 'ERROR: the DEPLOYED ${DEPLOY_DIR}/docker-compose.yml does not declare AUTO_UPGRADE_MODULES' >&2
    echo '       on the odoo service. Restarting odoo would advance the code WITHOUT running' >&2
    echo '       migrations -- a half-migrated production DB. This droplet predates the' >&2
    echo '       GOL-1009 entrypoint wiring (user_data is in ignore_changes, so terraform will' >&2
    echo '       not fix it in place). Hand-add' >&2
    echo '         AUTO_UPGRADE_MODULES: grove_headless,grove_support' >&2
    echo '       to the odoo service environment block (it is already in the source compose,' >&2
    echo '       so the edit is convergent and survives a rebuild), then re-run.' >&2
    exit 4
  fi
  echo \">> deployed compose AUTO_UPGRADE_MODULES = \$AUTO\"

  # --- pre-flight 5: on-disk manifest versions for the upgrade set ----------
  echo '>> on-disk manifest versions (pre-upgrade)'
  for mod in \$(printf '%s' \"\$AUTO\" | tr ',' ' '); do
    ver=\"\$(dc exec -T odoo grep -m1 version \"/workspace/current/\$mod/__manifest__.py\" 2>/dev/null | cut -d: -f2 | tr -cd '0-9.' || true)\"
    echo \"   \$mod: \${ver:-<unreadable>}\"
  done

  if [ \"\$CURRENT\" = \"\$TARGET\" ] && [ \"\$SYNCED\" = \"\$TARGET\" ] && [ \"\$LAST\" = \"\$TARGET\" ]; then
    echo
    echo \">> NO-OP: env pin, git-sync checkout and upgrade marker are all already \$TARGET.\"
    echo '>> Nothing to promote. (Re-running is safe; this is the idempotent path.)'
    exit 0
  fi

  if [ \"\$MODE\" != 'promote' ]; then
    echo
    echo '>> Pre-flight only -- stopping here, nothing written.'
    echo \">> Would set CUSTOM_MODULES_REF \${CURRENT:-<unset>} -> \$TARGET, resync, and restart odoo.\"
    echo '>> Re-run with CONFIRM=PROMOTE to execute.'
    exit 0
  fi

  ###########################################################################
  # PROMOTE
  ###########################################################################
  echo
  STAMP=\"\$(date -u +%Y%m%dT%H%M%SZ)\"
  BACKUP=\"${DEPLOY_DIR}/.env.bak.\$STAMP\"
  echo \">> backing up ${DEPLOY_DIR}/.env -> \$BACKUP (rollback source)\"
  cp -p '${DEPLOY_DIR}/.env' \"\$BACKUP\"

  # Idempotent upsert: rewrite the line if present, append it if not. Never
  # duplicate the key -- compose reads the LAST occurrence, so a duplicate is a
  # silent split-brain between what the file appears to say and what runs.
  if grep -q '^CUSTOM_MODULES_REF=' '${DEPLOY_DIR}/.env'; then
    sed -i \"s|^CUSTOM_MODULES_REF=.*|CUSTOM_MODULES_REF=\$TARGET|\" '${DEPLOY_DIR}/.env'
  else
    printf 'CUSTOM_MODULES_REF=%s\n' \"\$TARGET\" >> '${DEPLOY_DIR}/.env'
  fi
  WROTE=\"\$(sed -n 's/^CUSTOM_MODULES_REF=//p' '${DEPLOY_DIR}/.env' | tail -1 | tr -d '\r')\"
  [ \"\$WROTE\" = \"\$TARGET\" ] || { echo \"ERROR: .env upsert did not take (reads \$WROTE)\" >&2; exit 5; }
  [ \"\$(grep -c '^CUSTOM_MODULES_REF=' '${DEPLOY_DIR}/.env')\" = '1' ] \
    || { echo 'ERROR: duplicate CUSTOM_MODULES_REF lines in .env -- refusing to continue' >&2; exit 5; }
  echo \">> CUSTOM_MODULES_REF set to \$TARGET\"

  # GITSYNC_REF is resolved from the env file at CONTAINER CREATE time, not
  # read live -- a bare \`restart\` would keep the OLD ref. Force-recreate the
  # sidecar only (--no-deps keeps odoo untouched until the code is actually on
  # disk, so odoo never restarts against a half-synced worktree).
  echo '>> recreating custom-modules-sync so the new GITSYNC_REF takes effect'
  dc up -d --force-recreate --no-deps custom-modules-sync

  echo \">> waiting up to ${SYNC_TIMEOUT}s for git-sync to check out \$TARGET\"
  deadline=\$(( \$(date +%s) + ${SYNC_TIMEOUT} ))
  got=''
  while [ \"\$(date +%s)\" -lt \"\$deadline\" ]; do
    got=\"\$(dc exec -T custom-modules-sync git -C /workspace/current rev-parse HEAD 2>/dev/null | tr -d '\r\n' || true)\"
    [ \"\$got\" = \"\$TARGET\" ] && break
    sleep ${POLL_INTERVAL}
  done
  if [ \"\$got\" != \"\$TARGET\" ]; then
    echo \"ERROR: git-sync is on '\${got:-<unresolved>}', not \$TARGET, after ${SYNC_TIMEOUT}s.\" >&2
    echo '       Odoo was NOT restarted, so prod is still serving the previous code.' >&2
    echo \"       Roll back the env file with:  cp -p \$BACKUP ${DEPLOY_DIR}/.env && docker compose --env-file ${DEPLOY_DIR}/.env up -d --force-recreate --no-deps custom-modules-sync\" >&2
    exit 6
  fi
  echo \">> git-sync checkout confirmed at \$TARGET\"

  # The blocking upgrade runs in the entrypoint on boot: the revision advanced,
  # so it fires --init=base,<mods> --update=<mods> --stop-after-init BEFORE the
  # server starts, and writes the marker only on success (a failed migration
  # aborts boot and retries -- it never serves a half-migrated DB).
  echo '>> restarting odoo (entrypoint runs the blocking GOL-1009 upgrade pass)'
  dc restart odoo

  echo \">> waiting up to ${UPGRADE_TIMEOUT}s for the upgrade marker to reach \$TARGET\"
  deadline=\$(( \$(date +%s) + ${UPGRADE_TIMEOUT} ))
  while [ \"\$(date +%s)\" -lt \"\$deadline\" ]; do
    LAST=\"\$(cat '${MARKER}' 2>/dev/null | tr -d '\r\n' || true)\"
    [ \"\$LAST\" = \"\$TARGET\" ] && break
    sleep ${POLL_INTERVAL}
  done
  if [ \"\$LAST\" != \"\$TARGET\" ]; then
    echo \"ERROR: upgrade marker is '\${LAST:-<none>}', not \$TARGET, after ${UPGRADE_TIMEOUT}s.\" >&2
    echo '       The migration did not complete. Odoo logs (last 120 lines):' >&2
    dc logs --tail=120 odoo >&2 || true
    echo \"       Rollback: cp -p \$BACKUP ${DEPLOY_DIR}/.env && docker compose --env-file ${DEPLOY_DIR}/.env up -d --force-recreate --no-deps custom-modules-sync && docker compose --env-file ${DEPLOY_DIR}/.env restart odoo\" >&2
    exit 7
  fi
  echo \">> upgrade marker recorded \$TARGET -- migrations ran\"

  ###########################################################################
  # THE MONEY GUARD -- see this file's header.
  ###########################################################################
  echo
  echo '>> money guard: WV 6% state sales tax bind coverage'
  LOGS=\"\$(dc logs --since=\"${UPGRADE_TIMEOUT}s\" odoo 2>/dev/null || true)\"

  case \",\$AUTO,\" in
    *,grove_headless,*) NEED_TAX=1 ;;
    *) NEED_TAX=0 ;;
  esac

  if [ \"\$NEED_TAX\" = '1' ]; then
    # Match the ASCII prefix only. The partial-bind WARNING contains an em dash,
    # and a mojibake'd log (GOL-1646) must not make this guard silently miss.
    BIND_LINE=\"\$(printf '%s\n' \"\$LOGS\" | grep -F 'WV 6% state sales tax bound for' | tail -1 || true)\"
    if [ -z \"\$BIND_LINE\" ]; then
      echo 'ERROR: grove_headless was upgraded but NO \"WV 6% state sales tax bound for N of N\"' >&2
      echo '       line appeared in the log window. The tax hook did not run, or the log' >&2
      echo '       rolled past it. DO NOT take orders until this is resolved by hand:' >&2
      echo '       a company left on the old 7% / demo 15% tax mis-charges every order.' >&2
      exit 8
    fi
    echo \"   \$BIND_LINE\"
    if printf '%s' \"\$BIND_LINE\" | grep -q 'bound for only '; then
      echo 'ERROR: PARTIAL TAX BIND. At least one company kept its previous default sale tax.' >&2
      echo '       setup_wv_sales_tax swallows per-company failures at WARNING and still exits 0,' >&2
      echo '       so the upgrade LOOKS successful. It is not. The WARNINGs above the line name' >&2
      echo '       the company that failed. STOP THE PROMOTE and fix before prod takes orders.' >&2
      printf '%s\n' \"\$LOGS\" | grep -F 'WV tax setup FAILED for company' >&2 || true
      exit 9
    fi
    BOUND=\"\$(printf '%s' \"\$BIND_LINE\" | sed -n 's/.*bound for \([0-9]*\) of \([0-9]*\) companies.*/\1/p')\"
    TOTAL=\"\$(printf '%s' \"\$BIND_LINE\" | sed -n 's/.*bound for \([0-9]*\) of \([0-9]*\) companies.*/\2/p')\"
    if [ -z \"\$BOUND\" ] || [ -z \"\$TOTAL\" ] || [ \"\$BOUND\" != \"\$TOTAL\" ]; then
      echo \"ERROR: could not confirm a full bind from: \$BIND_LINE\" >&2
      exit 9
    fi
    echo \"   OK -- bound for \$BOUND of \$TOTAL companies (full coverage)\"
  else
    echo \"   skipped: grove_headless is not in AUTO_UPGRADE_MODULES (\$AUTO)\"
  fi

  echo
  echo '>> post-upgrade manifest versions'
  for mod in \$(printf '%s' \"\$AUTO\" | tr ',' ' '); do
    ver=\"\$(dc exec -T odoo grep -m1 version \"/workspace/current/\$mod/__manifest__.py\" 2>/dev/null | cut -d: -f2 | tr -cd '0-9.' || true)\"
    echo \"   \$mod: \${ver:-<unreadable>}\"
  done

  echo
  echo \">> PROMOTED: prod modules are live at \$TARGET.\"
  echo \">> env backup kept at \$BACKUP\"
  echo '>> Rollback (if needed):'
  echo \"     cp -p \$BACKUP ${DEPLOY_DIR}/.env\"
  echo \"     docker compose --env-file ${DEPLOY_DIR}/.env up -d --force-recreate --no-deps custom-modules-sync\"
  echo \"     rm -f ${MARKER} && docker compose --env-file ${DEPLOY_DIR}/.env restart odoo\"
  echo '   (clearing the marker forces the entrypoint to re-upgrade on the rolled-back code;'
  echo '    Odoo does not down-migrate, so treat a rollback as an incident, not a routine undo.)'
"

if [ "${MODE}" = "promote" ]; then
  cat <<EOF

== next steps (do NOT skip) ==
1. Verify installed_version from outside the box (docs/RUNBOOK-module-upgrade.md
   "Verify"): grove_headless should report the manifest version printed above.
2. Converge committed Terraform onto the now-live pin -- a catch-up PR, opened
   AFTER the live bump so it converges onto what prod IS serving (authoring it
   early recreates the #666/#667 competing-reconcile mess):
       gh workflow run reconcile-modules-pin.yml \\
         --repo Goldberry-Playground/odoocker-goldberrygrove \\
         -f modules_sha=${TARGET_REF} \\
         -f note='Train release promote (Leg B)'
   infra/terraform/environments/production/** is a protected-paths-guard glob:
   the PR needs SHA-bound human review. Merge != deploy -- prod is ALREADY on
   this SHA; the PR only stops the next rebuild rolling prod backward.
EOF
fi
