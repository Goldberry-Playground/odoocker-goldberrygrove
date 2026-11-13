#!/usr/bin/env bash
# prod-discord-events-apply.sh -- land the Discord calendar env on the LIVE prod
# Odoo droplet (GOL-3831, phase C of grove-odoo-modules#395
# docs/superpowers/specs/2026-11-07-discord-calendar-design.md).
#
# WHY A HUMAN RUNS THIS: the auto-mode classifier blocks agent SSH writes to
# prod, and prod's /etc/grove/{.env,docker-compose.yml} are rendered from
# cloud-init user_data that Terraform ignores (ignore_changes), so merging the
# TF chain in this repo is a provable no-op for the running box. Same shape as
# the 2026-10-30 topic-channel wiring (#967): this script is that hand edit,
# made idempotent, verified at the process level, and reversible.
#
# What it converges on the droplet (three points per key, like
# scripts/prod-modules-promote.sh pre-flight 9):
#   1. /etc/grove/.env        KEY=value lines for the values you pass in
#   2. docker-compose.yml     odoo `environment:` passthrough for all five keys
#                             (an unlisted key in .env NEVER reaches the process)
#   3. AUTO_UPGRADE_MODULES   appends grove_calendar so Leg B's rev-gated boot
#                             upgrade installs the module
# then recreates odoo (a plain `restart` keeps the OLD container env) and proves
# each key is PRESENT in the odoo process env.
#
# Usage, from your laptop (values come from 1Password, never from argv):
#
#   # read-only report, exit 0 = converged, 2 = something pending
#   op run --env-file=scripts/prod-discord-events.env.op -- scripts/prod-discord-events-apply.sh
#
#   # promote day, BEFORE Leg B (`TARGET_REF=... CONFIRM=PROMOTE scripts/prod-modules-promote.sh`)
#   TARGET_REF=<grove-odoo-modules 40-char sha> \
#     op run --env-file=scripts/prod-discord-events.env.op -- scripts/prod-discord-events-apply.sh --apply
#
#   # escape hatch: env lines only, leave AUTO_UPGRADE_MODULES alone (e.g. a
#   # rotated bot token, or grove_calendar already installed)
#   SKIP_MODULE=1 op run --env-file=scripts/prod-discord-events.env.op -- scripts/prod-discord-events-apply.sh --apply
#
# ORDER MATTERS: run --apply BEFORE Leg B. The recreate here happens on the
# CURRENT modules revision, so the entrypoint skips the upgrade (marker
# unchanged); Leg B then advances the pin and the rev-gated pass runs
# --init=base,<AUTO list incl. grove_calendar> on the new ref. If you ran this
# AFTER Leg B, install the module with the escape hatch instead:
# scripts/module-upgrade.sh (clears the marker + restarts odoo).
#
# GATE on the module: appending grove_calendar to AUTO_UPGRADE_MODULES on a pin
# whose tree lacks grove_calendar/__manifest__.py crash-loops odoo at boot. The
# script therefore refuses to touch the list unless `gh api` confirms the
# manifest exists at TARGET_REF (the sha you are about to promote to).
#
# Values: DISCORD_EVENTS_WEBHOOK_URL / DISCORD_BOT_TOKEN / DISCORD_GUILD_ID are
# each OPTIONAL. Unset or empty => that key's .env line is left exactly as it
# is (the passthrough is still ensured), so a re-run with a single rotated
# value touches only that line. On Train #5 all three are provided. Each value is
# validated locally against the same regex as the TF variable, travels to the
# droplet on STDIN (never argv, never the remote process list), and is never
# echoed -- only character counts are printed.
# GROVE_EVENTS_WINDOW_DAYS / GROVE_EVENTS_DEFAULT_ROOM get their defaults
# from the compose passthrough (60 / grove-chat: an event with a blank Location
# becomes a voice event in grove-chat); this script does not write them to .env.
#
# Rollback: the script backs up both files with a UTC stamp before writing and
# prints the exact `cp` + recreate to undo the run.
#
# Exit codes: 0 converged / applied+verified; 2 check mode, something pending;
# 3 local validation or pre-flight failure; 10 remote edit or verification
# failed (files restored where the script could).
set -euo pipefail

PROD_HOST="${PROD_HOST:-root@174.138.119.171}"   # DO reserved IP; the proxied hostname has no :22
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
TARGET_REF="${TARGET_REF:-}"
SKIP_MODULE="${SKIP_MODULE:-}"
MODULE="grove_calendar"
GOM_REPO="Goldberry-Playground/grove-odoo-modules"

MODE="check"
case "${1:-}" in
  ""|--check) MODE="check" ;;
  --apply)    MODE="apply" ;;
  -h|--help)  sed -n '2,70p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1 (use --check, --apply or --help)" >&2; exit 3 ;;
esac

die() { echo "ERROR: $*" >&2; exit 3; }

# --- local validation (never prints a value) ---------------------------------
validate() {
  _name="$1" _re="$2" _val="${3:-}"
  if [ -z "$_val" ]; then
    echo ">> $_name: not provided -> .env line left untouched (passthrough still ensured)"
    return 0
  fi
  if ! printf '%s' "$_val" | grep -Eq "$_re"; then
    die "$_name (${#_val} chars) does not match $_re -- bare value only, no quotes, no whitespace, no /slack suffix"
  fi
  echo ">> $_name: provided (${#_val} chars), will be written"
}
validate DISCORD_EVENTS_WEBHOOK_URL '^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+$' "${DISCORD_EVENTS_WEBHOOK_URL:-}"
validate DISCORD_BOT_TOKEN         '^[A-Za-z0-9_.-]+$'                                                              "${DISCORD_BOT_TOKEN:-}"
validate DISCORD_GUILD_ID          '^[0-9]{17,20}$'                                                                  "${DISCORD_GUILD_ID:-}"

# --- module gate ----------------------------------------------------------------
ADD_MODULE=0
if [ -n "$SKIP_MODULE" ]; then
  echo ">> SKIP_MODULE set: AUTO_UPGRADE_MODULES will not be touched"
elif [ "$MODE" = "apply" ]; then
  [ -n "$TARGET_REF" ] || die "TARGET_REF is required for --apply (the grove-odoo-modules sha Leg B will promote to), or pass SKIP_MODULE=1 to land env only"
  printf '%s' "$TARGET_REF" | grep -Eq '^[0-9a-f]{40}$' || die "TARGET_REF must be a 40-char lowercase sha"
  command -v gh >/dev/null 2>&1 || die "gh CLI is needed to prove TARGET_REF carries ${MODULE}/__manifest__.py (or SKIP_MODULE=1)"
  if gh api "repos/${GOM_REPO}/contents/${MODULE}/__manifest__.py?ref=${TARGET_REF}" --jq .path >/dev/null 2>&1; then
    echo ">> ${MODULE}/__manifest__.py present at ${GOM_REPO}@${TARGET_REF:0:12}: module append is SAFE"
    ADD_MODULE=1
  else
    die "${MODULE}/__manifest__.py is NOT at ${GOM_REPO}@${TARGET_REF:0:12}. Appending it to AUTO_UPGRADE_MODULES would crash-loop odoo at boot. Promote a ref that carries GOL-3830, or pass SKIP_MODULE=1."
  fi
else
  ADD_MODULE=1   # check mode only reports whether the module is in the list
fi

echo ">> target ${PROD_HOST}:${DEPLOY_DIR} -- mode ${MODE}"
echo

# --- remote ---------------------------------------------------------------------
# The whole remote program travels on STDIN as a single-quoted heredoc: nothing
# below REMOTE is expanded locally (no double-quoted ssh payload, so the
# GOL-2531 class of local-expansion leak cannot occur). The few locals the
# droplet needs are prepended as shell assignments, %q-quoted; the three
# values are among them, which is why they never appear in any argv.
# `main` is defined first and invoked last with stdin from /dev/null so no
# command inside can swallow the rest of the script off the ssh stream.
{
  printf 'MODE=%q\n'       "$MODE"
  printf 'DEPLOY_DIR=%q\n' "$DEPLOY_DIR"
  printf 'ADD_MODULE=%q\n' "$ADD_MODULE"
  printf 'MODULE=%q\n'     "$MODULE"
  printf 'export V_DISCORD_EVENTS_WEBHOOK_URL=%q\n' "${DISCORD_EVENTS_WEBHOOK_URL:-}"
  printf 'export V_DISCORD_BOT_TOKEN=%q\n'          "${DISCORD_BOT_TOKEN:-}"
  printf 'export V_DISCORD_GUILD_ID=%q\n'           "${DISCORD_GUILD_ID:-}"
  cat <<'REMOTE'
main() {
  set -euo pipefail
  cd "$DEPLOY_DIR"
  dc() { docker compose --env-file "$DEPLOY_DIR/.env" "$@"; }
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  SECRET_KEYS="DISCORD_EVENTS_WEBHOOK_URL DISCORD_BOT_TOKEN DISCORD_GUILD_ID"
  ALL_KEYS="$SECRET_KEYS GROVE_EVENTS_WINDOW_DAYS GROVE_EVENTS_DEFAULT_ROOM"
  echo ">> $(hostname) $STAMP -- Discord calendar env (GOL-3831), mode $MODE"
  [ -f .env ] || { echo "ERROR: $DEPLOY_DIR/.env missing" >&2; exit 10; }
  [ -f docker-compose.yml ] || { echo "ERROR: $DEPLOY_DIR/docker-compose.yml missing" >&2; exit 10; }

  AUTO="$(grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' docker-compose.yml | head -1 | cut -d: -f2- | tr -d ' \r' || true)"
  if [ -z "$AUTO" ]; then
    echo "ERROR: deployed compose declares no AUTO_UPGRADE_MODULES on the odoo service; fix that first (see scripts/prod-modules-promote.sh pre-flight 4)" >&2
    exit 10
  fi
  echo ">> deployed compose AUTO_UPGRADE_MODULES = $AUTO"
  SYNCED="$(dc exec -T custom-modules-sync readlink -f /workspace/current 2>/dev/null | tr -d '\r' || true)"
  echo ">> git-sync /workspace/current = ${SYNCED:-<unresolved>}"

  PENDING=0
  for K in $ALL_KEYS; do
    COUNT="$(grep -c "^$K=" .env || true)"
    CUR="$(sed -n "s/^$K=//p" .env | tail -1 | tr -d '\r')"
    COMPOSE=no; grep -Eq "^[[:space:]]*$K:" docker-compose.yml && COMPOSE=yes
    RUNTIME=absent; dc exec -T odoo printenv "$K" >/dev/null 2>&1 && RUNTIME=present
    case "$K" in
      GROVE_EVENTS_*) echo ">> $K: .env = '${CUR:-<unset>}' (x$COUNT), compose passthrough = $COMPOSE, odoo process env = $RUNTIME" ;;
      *) if [ -n "$CUR" ]; then ST="set (${#CUR} chars)"; elif [ "$COUNT" != 0 ]; then ST=empty; else ST=unset; fi
         echo ">> $K: .env = $ST (x$COUNT), compose passthrough = $COMPOSE, odoo process env = $RUNTIME" ;;
    esac
    if [ "$COMPOSE" != yes ] || [ "$RUNTIME" != present ]; then
      PENDING=1; echo "   -> passthrough PENDING"
    fi
    case " $SECRET_KEYS " in *" $K "*)
      WANT="$(eval "printf '%s' \"\${V_$K:-}\"")"
      if [ -n "$WANT" ] && [ "$WANT" != "$CUR" ]; then
        PENDING=1; echo "   -> .env write PENDING (${#WANT} chars)"
      fi ;;
    esac
  done
  case ",$AUTO," in
    *,"$MODULE",*) echo ">> $MODULE already in AUTO_UPGRADE_MODULES" ;;
    *) echo ">> $MODULE NOT in AUTO_UPGRADE_MODULES"
       if [ "$ADD_MODULE" = 1 ]; then PENDING=1; echo "   -> module append PENDING"; fi ;;
  esac

  if [ "$MODE" != apply ]; then
    echo
    if [ "$PENDING" = 1 ]; then echo "RESULT: pending changes above (re-run with --apply)"; exit 2; fi
    echo "RESULT: converged, nothing to do"; exit 0
  fi
  if [ "$PENDING" = 0 ]; then echo; echo "RESULT: already converged, nothing written"; exit 0; fi

  echo
  echo ">> backing up .env and docker-compose.yml (.bak.$STAMP)"
  cp -p .env ".env.bak.$STAMP"
  cp -p docker-compose.yml "docker-compose.yml.bak.$STAMP"
  restore() {
    cp -p ".env.bak.$STAMP" .env
    cp -p "docker-compose.yml.bak.$STAMP" docker-compose.yml
    echo "       both files restored from .bak.$STAMP; nothing recreated." >&2
  }

  # 1. .env upsert: replace an existing KEY= line in place, else append. The
  #    value reaches python through the environment, never argv.
  for K in $SECRET_KEYS; do
    WANT="$(eval "printf '%s' \"\${V_$K:-}\"")"
    [ -n "$WANT" ] || continue
    K="$K" python3 - .env <<'PY' || { restore; exit 10; }
import os, sys
path = sys.argv[1]; key = os.environ["K"]; val = os.environ["V_" + key]
lines = open(path).read().splitlines(keepends=True)
new = f"{key}={val}\n"; hit = False
for i, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[i] = new; hit = True
if not hit:
    if lines and not lines[-1].endswith("\n"): lines[-1] += "\n"
    lines.append(new)
open(path, "w").writelines(lines)
print(f"   .env: {'replaced' if hit else 'appended'} {key}")
PY
  done

  # 2. compose passthrough: insert after DISCORD_SHIPPING_WEBHOOK_URL (the #967
  #    block) when present, else after AUTO_UPGRADE_MODULES. Only the ${...}
  #    interpolation is written -- no value ever touches this file.
  python3 - docker-compose.yml <<'PY' || { restore; exit 10; }
import re, sys
path = sys.argv[1]
want = [("DISCORD_EVENTS_WEBHOOK_URL", ""), ("DISCORD_BOT_TOKEN", ""), ("DISCORD_GUILD_ID", ""),
        ("GROVE_EVENTS_WINDOW_DAYS", "60"), ("GROVE_EVENTS_DEFAULT_ROOM", "grove-chat")]
lines = open(path).read().splitlines(keepends=True)
def has(key): return any(re.match(r"^\s*" + re.escape(key) + r":", l) for l in lines)
def anchor():
    for i, l in enumerate(lines):
        if re.match(r"^(\s*)DISCORD_SHIPPING_WEBHOOK_URL:", l): return i
    for i, l in enumerate(lines):
        if re.match(r"^(\s*)AUTO_UPGRADE_MODULES:", l): return i
    raise SystemExit("compose: no DISCORD_SHIPPING_WEBHOOK_URL or AUTO_UPGRADE_MODULES anchor")
at = anchor(); indent = re.match(r"^(\s*)", lines[at]).group(1)
for key, default in want:
    if has(key):
        print(f"   compose: {key} already present"); continue
    at += 1
    lines.insert(at, f"{indent}{key}: ${{{key}:-{default}}}\n")
    print(f"   compose: inserted {key}")
open(path, "w").writelines(lines)
PY

  # 3. AUTO_UPGRADE_MODULES append (gated locally on TARGET_REF carrying the manifest)
  if [ "$ADD_MODULE" = 1 ]; then
    MODULE="$MODULE" python3 - docker-compose.yml <<'PY' || { restore; exit 10; }
import os, re, sys
path = sys.argv[1]; mod = os.environ["MODULE"]
lines = open(path).read().splitlines(keepends=True)
for i, l in enumerate(lines):
    m = re.match(r"^(\s*AUTO_UPGRADE_MODULES:\s*)(\S+)(\s*)$", l)
    if m:
        mods = m.group(2).split(",")
        if mod in mods: print(f"   compose: {mod} already in AUTO_UPGRADE_MODULES"); break
        mods.append(mod)
        lines[i] = m.group(1) + ",".join(mods) + "\n"
        print(f"   compose: AUTO_UPGRADE_MODULES -> {','.join(mods)}"); break
else:
    raise SystemExit("compose: AUTO_UPGRADE_MODULES line not found")
open(path, "w").writelines(lines)
PY
  fi

  # 4. fail closed on a compose the daemon cannot parse
  if ! dc config -q >/dev/null 2>&1; then
    echo "ERROR: edited docker-compose.yml failed 'docker compose config'" >&2
    restore; exit 10
  fi
  echo ">> compose validates; recreating odoo (same modules revision => entrypoint skips the upgrade)"
  dc up -d --force-recreate --no-deps odoo
  for _ in $(seq 1 30); do
    dc exec -T odoo true >/dev/null 2>&1 && break
    sleep 2
  done

  echo ">> verifying the odoo process env"
  FAIL=0
  for K in $ALL_KEYS; do
    if GOT="$(dc exec -T odoo printenv "$K" 2>/dev/null | tr -d '\r\n')"; then
      case "$K" in
        GROVE_EVENTS_*) echo "   OK -- $K='$GOT'" ;;
        *) echo "   OK -- $K present (${#GOT} chars)" ;;
      esac
    else
      echo "   FAIL -- $K absent from the odoo process env" >&2; FAIL=1
    fi
  done
  AUTO_NOW="$(grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' docker-compose.yml | head -1 | cut -d: -f2- | tr -d ' \r')"
  echo "   deployed compose AUTO_UPGRADE_MODULES = $AUTO_NOW"
  if [ "$ADD_MODULE" = 1 ]; then
    case ",$AUTO_NOW," in *,"$MODULE",*) echo "   OK -- $MODULE in AUTO_UPGRADE_MODULES (installs on the next revision advance = Leg B)" ;;
      *) echo "   FAIL -- $MODULE missing from AUTO_UPGRADE_MODULES" >&2; FAIL=1 ;; esac
  fi
  echo
  echo "Rollback (undoes this whole run):"
  echo "  cp -p $DEPLOY_DIR/.env.bak.$STAMP $DEPLOY_DIR/.env && cp -p $DEPLOY_DIR/docker-compose.yml.bak.$STAMP $DEPLOY_DIR/docker-compose.yml \\"
  echo "    && docker compose --env-file $DEPLOY_DIR/.env up -d --force-recreate --no-deps odoo"
  if [ "$FAIL" != 0 ]; then echo "RESULT: FAILED verification -- see FAIL lines; files left in place for inspection" >&2; exit 10; fi
  echo "RESULT: applied and verified. Next: Leg B (TARGET_REF=... CONFIRM=PROMOTE scripts/prod-modules-promote.sh)."
}
main </dev/null
REMOTE
} | ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$PROD_HOST" 'bash -s'
