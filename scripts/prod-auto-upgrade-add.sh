#!/usr/bin/env bash
# prod-auto-upgrade-add.sh -- append one Grove module to AUTO_UPGRADE_MODULES in
# the DEPLOYED compose on the live prod Odoo droplet, so the next modules
# revision advance (Leg B of a promote) installs it.
#
# WHY THIS EXISTS: prod's /etc/grove/docker-compose.yml is a hand copy rendered
# once from cloud-init (Terraform ignores user_data), so adding a module to the
# repo compose changes nothing on the running box. Same reason as
# scripts/prod-discord-events-apply.sh, which does this for grove_calendar.
#
# Written for grove_schedule_f (farm books, grove-odoo-modules#390). That module
# depends on OCA addons that ship in the IMAGE (odoo/third-party-addons.txt),
# not in the git-synced tree. If the running image predates them, the boot
# `--init` fails and odoo crash-loops. So this script refuses to touch the list
# unless BOTH gates pass:
#   1. `gh api` proves MODULE/__manifest__.py exists at TARGET_REF
#   2. every name in REQUIRE_ADDONS is a directory inside the running odoo
#      container
# If gate 2 fails the fix is a newer image on the box, which is a separate,
# deliberate step. This script never pulls an image.
#
# Usage, from your laptop on the admin IP:
#
#   # read-only report: exit 0 = already in the list, 2 = pending or gate failed
#   scripts/prod-auto-upgrade-add.sh
#
#   # promote day, BEFORE Leg B
#   TARGET_REF=<grove-odoo-modules 40-char sha> scripts/prod-auto-upgrade-add.sh --apply
#
# ORDER: run --apply BEFORE Leg B. After the edit the script recreates odoo (a
# plain restart keeps the OLD container env, and Leg B may only restart). The
# recreate happens on the CURRENT modules revision, so the entrypoint skips the
# upgrade; Leg B then advances the pin and runs --init=base,<new list>. If you
# run it AFTER Leg B, follow with scripts/module-upgrade.sh (clears the marker
# and restarts odoo).
#
# NO_RECREATE=1 skips the recreate. Use it only when
# scripts/prod-discord-events-apply.sh --apply runs right after, since that
# script recreates odoo itself and one blip is better than two.
#
# Other modules: MODULE=<name> REQUIRE_ADDONS="<addon> <addon>" (empty string
# when the module needs nothing from the image).
#
# Rollback: the script backs up the compose with a UTC stamp and prints the cp.
#
# Exit codes: 0 in the list (already, or applied and verified); 2 check mode,
# pending or a gate failed; 3 local validation failure; 10 remote failure
# (file restored where the script could).
set -euo pipefail

PROD_HOST="${PROD_HOST:-root@174.138.119.171}"   # DO reserved IP; the proxied hostname has no :22
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
TARGET_REF="${TARGET_REF:-}"
MODULE="${MODULE:-grove_schedule_f}"
REQUIRE_ADDONS="${REQUIRE_ADDONS-account_reconcile_oca account_statement_import_sheet_file}"
NO_RECREATE="${NO_RECREATE:-}"
GOM_REPO="Goldberry-Playground/grove-odoo-modules"

MODE="check"
case "${1:-}" in
  ""|--check) MODE="check" ;;
  --apply)    MODE="apply" ;;
  -h|--help)  sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1 (use --check, --apply or --help)" >&2; exit 3 ;;
esac

die() { echo "ERROR: $*" >&2; exit 3; }

printf '%s' "$MODULE" | grep -Eq '^[a-z][a-z0-9_]*$' || die "MODULE must be a bare module name"
for A in $REQUIRE_ADDONS; do
  printf '%s' "$A" | grep -Eq '^[a-z][a-z0-9_]*$' || die "REQUIRE_ADDONS entry '$A' is not a bare module name"
done

# --- gate 1: the module is in the tree Leg B will promote to -------------------
if [ "$MODE" = "apply" ]; then
  [ -n "$TARGET_REF" ] || die "TARGET_REF is required for --apply (the grove-odoo-modules sha Leg B will promote to)"
  printf '%s' "$TARGET_REF" | grep -Eq '^[0-9a-f]{40}$' || die "TARGET_REF must be a 40-char lowercase sha"
  command -v gh >/dev/null 2>&1 || die "gh CLI is needed to prove TARGET_REF carries ${MODULE}/__manifest__.py"
  gh api "repos/${GOM_REPO}/contents/${MODULE}/__manifest__.py?ref=${TARGET_REF}" --jq .path >/dev/null 2>&1 \
    || die "${MODULE}/__manifest__.py is NOT at ${GOM_REPO}@${TARGET_REF:0:12}. Appending it would crash-loop odoo at boot."
  echo ">> gate 1 OK: ${MODULE}/__manifest__.py present at ${GOM_REPO}@${TARGET_REF:0:12}"
fi

echo ">> target ${PROD_HOST}:${DEPLOY_DIR} -- mode ${MODE}, module ${MODULE}"
echo

# The remote program travels on STDIN as a single-quoted heredoc, so nothing
# below REMOTE is expanded locally. `main` runs last with stdin from /dev/null
# so no command inside can swallow the rest of the script off the ssh stream.
{
  printf 'MODE=%q\n'           "$MODE"
  printf 'DEPLOY_DIR=%q\n'     "$DEPLOY_DIR"
  printf 'MODULE=%q\n'         "$MODULE"
  printf 'REQUIRE_ADDONS=%q\n' "$REQUIRE_ADDONS"
  printf 'NO_RECREATE=%q\n'    "$NO_RECREATE"
  cat <<'REMOTE'
set -euo pipefail
main() {
  cd "$DEPLOY_DIR"
  dc() { docker compose --env-file "$DEPLOY_DIR/.env" "$@"; }
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  echo ">> $(hostname) $STAMP -- AUTO_UPGRADE_MODULES += $MODULE, mode $MODE"
  [ -f docker-compose.yml ] || { echo "ERROR: $DEPLOY_DIR/docker-compose.yml missing" >&2; exit 10; }

  AUTO="$(grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' docker-compose.yml | head -1 | cut -d: -f2- | tr -d ' \r' || true)"
  if [ -z "$AUTO" ]; then
    echo "ERROR: deployed compose declares no AUTO_UPGRADE_MODULES on the odoo service (see scripts/prod-modules-promote.sh pre-flight 4)" >&2
    exit 10
  fi
  echo ">> deployed compose AUTO_UPGRADE_MODULES = $AUTO"
  echo ">> odoo image = $(dc images odoo 2>/dev/null | tail -n +2 | tr -s ' ' | head -1 || true)"

  case ",$AUTO," in
    *,"$MODULE",*) echo; echo "RESULT: $MODULE already in AUTO_UPGRADE_MODULES, nothing to do"; exit 0 ;;
  esac
  echo ">> $MODULE NOT in AUTO_UPGRADE_MODULES"

  # --- gate 2: the image carries the addons the module depends on ---------------
  MISSING=""
  for A in $REQUIRE_ADDONS; do
    HIT="$(dc exec -T odoo sh -c "find / -xdev -maxdepth 5 -type d -name '$A' 2>/dev/null | head -1" 2>/dev/null | tr -d '\r' || true)"
    if [ -n "$HIT" ] && dc exec -T odoo test -f "$HIT/__manifest__.py" 2>/dev/null; then
      echo ">> gate 2 OK: $A at $HIT"
    else
      echo ">> gate 2 FAIL: $A not found inside the running odoo container" >&2
      MISSING="$MISSING $A"
    fi
  done
  if [ -n "$MISSING" ]; then
    echo >&2
    echo "RESULT: REFUSED. The running image lacks:$MISSING" >&2
    echo "  Installing $MODULE now would crash-loop odoo on the boot --init." >&2
    echo "  The box needs a grove-odoo image built after odoo/third-party-addons.txt gained these addons." >&2
    echo "  Nothing was written." >&2
    exit 2
  fi

  if [ "$MODE" != apply ]; then
    echo; echo "RESULT: gates pass, append PENDING (re-run with TARGET_REF=... --apply)"; exit 2
  fi

  cp -p docker-compose.yml "docker-compose.yml.bak.$STAMP"
  restore() { cp -p "docker-compose.yml.bak.$STAMP" docker-compose.yml; echo ">> restored docker-compose.yml from backup" >&2; }

  MODULE="$MODULE" python3 - docker-compose.yml <<'PY' || { restore; exit 10; }
import os, re, sys
path = sys.argv[1]; mod = os.environ["MODULE"]
lines = open(path).read().splitlines(keepends=True)
for i, l in enumerate(lines):
    m = re.match(r"^(\s*AUTO_UPGRADE_MODULES:\s*)(\S+)(\s*)$", l)
    if m:
        mods = m.group(2).split(",")
        if mod not in mods:
            mods.append(mod)
            lines[i] = m.group(1) + ",".join(mods) + "\n"
        print(f"   compose: AUTO_UPGRADE_MODULES -> {','.join(mods)}"); break
else:
    raise SystemExit("compose: AUTO_UPGRADE_MODULES line not found")
open(path, "w").writelines(lines)
PY

  if ! dc config -q >/dev/null 2>&1; then
    echo "ERROR: edited docker-compose.yml failed 'docker compose config'" >&2
    restore; exit 10
  fi
  AUTO_NOW="$(grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' docker-compose.yml | head -1 | cut -d: -f2- | tr -d ' \r')"
  case ",$AUTO_NOW," in
    *,"$MODULE",*) ;;
    *) echo "ERROR: $MODULE missing from AUTO_UPGRADE_MODULES after the edit" >&2; restore; exit 10 ;;
  esac
  echo
  echo "Rollback:"
  echo "  cp -p $DEPLOY_DIR/docker-compose.yml.bak.$STAMP $DEPLOY_DIR/docker-compose.yml \\"
  echo "    && docker compose --env-file $DEPLOY_DIR/.env up -d --force-recreate --no-deps odoo"
  if [ -n "$NO_RECREATE" ]; then
    echo "RESULT: file edited, odoo NOT recreated (NO_RECREATE). The list reaches odoo only on its next recreate; run prod-discord-events-apply.sh --apply now, before Leg B."
    exit 0
  fi
  echo ">> recreating odoo (same modules revision => entrypoint skips the upgrade)"
  dc up -d --force-recreate --no-deps odoo
  for _ in $(seq 1 30); do
    dc exec -T odoo true >/dev/null 2>&1 && break
    sleep 2
  done
  GOT="$(dc exec -T odoo printenv AUTO_UPGRADE_MODULES 2>/dev/null | tr -d '\r\n' || true)"
  case ",$GOT," in
    *,"$MODULE",*) echo "   OK -- odoo process env AUTO_UPGRADE_MODULES = $GOT" ;;
    *) echo "RESULT: FAILED verification -- odoo process env AUTO_UPGRADE_MODULES = '${GOT:-<absent>}'; file left in place for inspection" >&2; exit 10 ;;
  esac
  echo "RESULT: applied and verified. $MODULE installs when Leg B advances the revision."
}
main </dev/null
REMOTE
} | ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$PROD_HOST" 'bash -s'
