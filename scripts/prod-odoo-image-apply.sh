#!/usr/bin/env bash
# prod-odoo-image-apply.sh -- move a LIVE Odoo droplet (prod, or QA for the
# rehearsal) onto a pinned grove-odoo image, put the image's third-party-addons
# dir on ADDONS_PATH, and run the core upgrade pass the new image needs.
#
# WHY THIS EXISTS: Terraform ignores user_data on the prod droplet, so
# var.odoo_image_tag and the cloud-init ADDONS_PATH line only reach a REBUILT
# box. On the running one, /etc/grove/.env is a hand copy: ODOO_TAG=latest
# resolves to whatever was pulled when the box was built (prod served base
# 19.0-20260513 on 2026-11-19, six months behind main), and ADDONS_PATH has no
# third-party-addons entry, so the OCA addons in the image are invisible and
# grove_schedule_f cannot install (GOL-3939, GOL-3940).
#
# What --apply does, in order. Nothing is written before step 4.
#   1. pull ghcr.io/goldberry-playground/grove-odoo:IMAGE_TAG and REFUSE unless
#      its registry digest equals EXPECT_DIGEST (the digest QA was gated on)
#   2. REFUSE unless the pulled image carries every REQUIRE_ADDONS manifest
#      under third-party-addons
#   3. tag the image the box runs now as grove-odoo:rollback-<stamp>, so the
#      way back does not depend on the registry
#   4. back up .env, set ODOO_TAG=IMAGE_TAG, add third-party-addons to
#      ADDONS_PATH, validate with `docker compose config`
#   5. recreate odoo on the new image and wait for /web/login
#   6. run `odoo -u UPGRADE_MODULES --stop-after-init` (default: all) inside
#      the new container, log kept on the box; FAIL on a non-zero exit, any
#      CRITICAL line, or more ERROR lines than MAX_ERROR_LINES (default 0)
#   7. restart odoo, wait for /web/login, verify image digest + addons_path
# If step 5, 6 or 7 fails the script restores .env onto the rollback tag and
# recreates odoo on the OLD image (KEEP_ON_FAILURE=1 leaves the new one up for
# debugging, use it on QA only). A failed -u may have committed some modules:
# old code on that DB is the same 19.0 series and normally serves, but if the
# smoke checks disagree, restore Managed PG to the PITR point the script prints.
#
# Usage, from your laptop on the admin IP:
#
#   # read-only report: image, ADDONS_PATH, Stripe providers.
#   # exit 0 = converged, 2 = pending
#   IMAGE_TAG=<odoocker 40-char sha> EXPECT_DIGEST=sha256:<64 hex> \
#     scripts/prod-odoo-image-apply.sh
#
#   # QA rehearsal (Monday of the train, after train-up)
#   PROD_HOST=root@odoo.qa.gatheringatthegrove.com \
#   IMAGE_TAG=<sha> EXPECT_DIGEST=sha256:<...> scripts/prod-odoo-image-apply.sh --apply
#
#   # prod, promote day, BEFORE scripts/prod-auto-upgrade-add.sh and Leg B
#   IMAGE_TAG=<sha> EXPECT_DIGEST=sha256:<...> scripts/prod-odoo-image-apply.sh --apply
#
# IMAGE_TAG is the odoocker commit sha docker-odoo.yml pushed (never `latest`).
# Read the digest for a tag without docker:
#   curl -sI -H "Authorization: Bearer $(curl -s 'https://ghcr.io/token?scope=repository:goldberry-playground/grove-odoo:pull' | jq -r .token)" \
#     -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json' \
#     https://ghcr.io/v2/goldberry-playground/grove-odoo/manifests/<sha> | grep -i docker-content-digest
#
# Knobs: SKIP_UPGRADE=1 swaps the image and path only (no -u pass).
# UPGRADE_MODULES=<list> narrows the pass. MAX_ERROR_LINES=<n> tolerates ERROR
# lines the QA rehearsal showed to be harmless. REQUIRE_ADDONS="" skips gate 2.
# HEALTH_TIMEOUT=<seconds> (default 300). FORCE_UPGRADE=1 runs the recreate and
# the -u pass even when the box is already on the target image (a QA box that
# train-up built on the pinned tag still has a DB from the old base).
#
# Downtime: odoo is recreated once and restarted once, and the -u pass runs
# against the live DB in between (about as long as a QA `-u all`). Checkout
# should be treated as down for the whole run.
#
# Exit codes: 0 converged (already, or applied and verified); 2 check mode
# pending, or a gate refused (nothing written); 3 local validation failure;
# 10 remote failure (rolled back where the script could).
# PROD_HOST defaults to the `grove-prod-odoo` ~/.ssh/config alias, same as
# scripts/prod-auto-upgrade-add.sh.
set -euo pipefail

PROD_HOST="${PROD_HOST:-grove-prod-odoo}"   # ~/.ssh/config alias; the proxied hostname has no :22
DEPLOY_DIR="${DEPLOY_DIR:-/etc/grove}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/goldberry-playground/grove-odoo}"
IMAGE_TAG="${IMAGE_TAG:-}"
EXPECT_DIGEST="${EXPECT_DIGEST:-}"
THIRD_PARTY_DIR="${THIRD_PARTY_DIR:-/usr/lib/python3/dist-packages/odoo/third-party-addons}"
REQUIRE_ADDONS="${REQUIRE_ADDONS-account_reconcile_oca account_statement_import_sheet_file}"
UPGRADE_MODULES="${UPGRADE_MODULES:-all}"
SKIP_UPGRADE="${SKIP_UPGRADE:-}"
MAX_ERROR_LINES="${MAX_ERROR_LINES:-0}"
KEEP_ON_FAILURE="${KEEP_ON_FAILURE:-}"
FORCE_UPGRADE="${FORCE_UPGRADE:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"

MODE="check"
case "${1:-}" in
  ""|--check) MODE="check" ;;
  --apply)    MODE="apply" ;;
  -h|--help)  sed -n '2,69p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1 (use --check, --apply or --help)" >&2; exit 3 ;;
esac

die() { echo "ERROR: $*" >&2; exit 3; }

if [ -n "$IMAGE_TAG" ]; then
  printf '%s' "$IMAGE_TAG" | grep -Eq '^[0-9a-f]{40}$' \
    || die "IMAGE_TAG must be the 40-char odoocker commit sha docker-odoo.yml pushed (got: $IMAGE_TAG). A moving tag such as latest cannot be pinned or rolled back."
fi
if [ -n "$EXPECT_DIGEST" ]; then
  printf '%s' "$EXPECT_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || die "EXPECT_DIGEST must look like sha256:<64 hex>"
fi
if [ "$MODE" = "apply" ]; then
  [ -n "$IMAGE_TAG" ] || die "IMAGE_TAG is required for --apply"
  [ -n "$EXPECT_DIGEST" ] || die "EXPECT_DIGEST is required for --apply (the digest the QA gate ran on)"
fi
for A in $REQUIRE_ADDONS; do
  printf '%s' "$A" | grep -Eq '^[a-z][a-z0-9_]*$' || die "REQUIRE_ADDONS entry '$A' is not a bare module name"
done
printf '%s' "$UPGRADE_MODULES" | grep -Eq '^[a-z][a-z0-9_]*(,[a-z][a-z0-9_]*)*$' || die "UPGRADE_MODULES must be 'all' or a comma list of module names"
printf '%s' "$MAX_ERROR_LINES" | grep -Eq '^[0-9]+$' || die "MAX_ERROR_LINES must be a number"
printf '%s' "$HEALTH_TIMEOUT" | grep -Eq '^[0-9]+$' || die "HEALTH_TIMEOUT must be a number of seconds"
printf '%s' "$THIRD_PARTY_DIR" | grep -Eq '^/[A-Za-z0-9_./-]+$' || die "THIRD_PARTY_DIR must be an absolute path"

echo ">> target ${PROD_HOST}:${DEPLOY_DIR} -- mode ${MODE}, image ${IMAGE_REPO}:${IMAGE_TAG:-<not given>}"
echo

# The remote program travels on STDIN as a single-quoted heredoc, so nothing
# below REMOTE is expanded locally. `main` runs last with stdin from /dev/null
# so no command inside can swallow the rest of the script off the ssh stream.
{
  printf 'MODE=%q\n'            "$MODE"
  printf 'DEPLOY_DIR=%q\n'      "$DEPLOY_DIR"
  printf 'IMAGE_REPO=%q\n'      "$IMAGE_REPO"
  printf 'IMAGE_TAG=%q\n'       "$IMAGE_TAG"
  printf 'EXPECT_DIGEST=%q\n'   "$EXPECT_DIGEST"
  printf 'TP_DIR=%q\n'          "$THIRD_PARTY_DIR"
  printf 'REQUIRE_ADDONS=%q\n'  "$REQUIRE_ADDONS"
  printf 'UPGRADE_MODULES=%q\n' "$UPGRADE_MODULES"
  printf 'SKIP_UPGRADE=%q\n'    "$SKIP_UPGRADE"
  printf 'MAX_ERROR_LINES=%q\n' "$MAX_ERROR_LINES"
  printf 'KEEP_ON_FAILURE=%q\n' "$KEEP_ON_FAILURE"
  printf 'FORCE_UPGRADE=%q\n'   "$FORCE_UPGRADE"
  printf 'HEALTH_TIMEOUT=%q\n'  "$HEALTH_TIMEOUT"
  cat <<'REMOTE'
set -euo pipefail
main() {
  cd "$DEPLOY_DIR"
  dc() { docker compose --env-file "$DEPLOY_DIR/.env" "$@"; }
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  echo ">> $(hostname) $STAMP -- odoo image step, mode $MODE"
  [ -f docker-compose.yml ] || { echo "ERROR: $DEPLOY_DIR/docker-compose.yml missing" >&2; exit 10; }
  [ -f .env ] || { echo "ERROR: $DEPLOY_DIR/.env missing" >&2; exit 10; }

  env_get() { sed -n "s/^$1=//p" .env | head -1 | tr -d '\r'; }
  running_image_id() {
    local cid
    cid="$(dc ps -q odoo 2>/dev/null | head -1 || true)"
    [ -n "$cid" ] || return 0
    docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true
  }
  digests_of() { docker image inspect --format '{{join .RepoDigests " "}}' "$1" 2>/dev/null || true; }
  healthy() {
    dc exec -T odoo python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8069/web/login", timeout=5)' >/dev/null 2>&1
  }
  wait_healthy() {
    local waited=0
    while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
      if healthy; then echo "   OK -- /web/login answers after ${waited}s"; return 0; fi
      sleep 5; waited=$((waited + 5))
    done
    echo "ERROR: odoo did not answer /web/login within ${HEALTH_TIMEOUT}s" >&2
    dc logs --tail=40 odoo 2>&1 | sed 's/^/   | /' >&2 || true
    return 1
  }

  CUR_TAG="$(env_get ODOO_TAG)"
  CUR_PATH="$(env_get ADDONS_PATH)"
  CUR_ID="$(running_image_id)"
  CUR_DIGESTS=""
  [ -n "$CUR_ID" ] && CUR_DIGESTS="$(digests_of "$CUR_ID")"
  echo ">> .env ODOO_TAG     = ${CUR_TAG:-<unset, compose default latest>}"
  echo ">> .env ADDONS_PATH  = ${CUR_PATH:-<unset>}"
  echo ">> running image id  = ${CUR_ID:-<no odoo container>}"
  echo ">> running digests   = ${CUR_DIGESTS:-<none>}"
  echo ">> running odoo      = $(dc exec -T odoo odoo --version 2>/dev/null | tr -d '\r' | head -1 || true)"

  PATH_OK=no
  case ",$CUR_PATH," in *,"$TP_DIR",*) PATH_OK=yes ;; esac
  TAG_OK=no
  [ -n "$IMAGE_TAG" ] && [ "$CUR_TAG" = "$IMAGE_TAG" ] && TAG_OK=yes
  DIGEST_OK=no
  if [ -n "$EXPECT_DIGEST" ]; then
    case " $CUR_DIGESTS " in *"@$EXPECT_DIGEST "*) DIGEST_OK=yes ;; esac
  fi
  echo ">> ADDONS_PATH carries third-party-addons: $PATH_OK; ODOO_TAG pinned to target: $TAG_OK; running digest is target: $DIGEST_OK"

  if [ "$MODE" != apply ]; then
    # Stripe providers (#997: payment_stripe answers webhooks 403 when the
    # provider has no webhook secret). Read-only, best effort.
    DB_NAME_R="$(env_get DB_NAME)"
    echo ">> payment.provider code=stripe (id | state | company | webhook secret):"
    dc exec -T odoo odoo shell --database="$DB_NAME_R" --no-http --log-level=warn 2>/dev/null <<'PY' | sed 's/^/   /' || echo "   WARN: could not read payment.provider (odoo shell failed)"
for p in env["payment.provider"].sudo().with_context(active_test=False).search([("code", "=", "stripe")], order="id"):
    print("%s | %s | %s | %s" % (p.id, p.state, p.company_id.name, "set" if p.stripe_webhook_secret else "EMPTY"))
PY
    echo
    if [ "$PATH_OK" = yes ] && [ "$TAG_OK" = yes ] && [ "$DIGEST_OK" = yes ]; then
      echo "RESULT: converged, nothing to do"; exit 0
    fi
    echo "RESULT: PENDING (re-run with IMAGE_TAG=... EXPECT_DIGEST=... --apply)"; exit 2
  fi

  if [ "$PATH_OK" = yes ] && [ "$TAG_OK" = yes ] && [ "$DIGEST_OK" = yes ]; then
    if [ -z "$FORCE_UPGRADE" ]; then
      echo; echo "RESULT: converged, nothing to do"; exit 0
    fi
    echo ">> converged, but FORCE_UPGRADE is set: recreating and running the -u pass anyway"
  fi

  NEW_REF="$IMAGE_REPO:$IMAGE_TAG"

  # --- gate 1: the tag is the image QA was gated on ---------------------------
  echo ">> pulling $NEW_REF"
  docker pull -q "$NEW_REF" >/dev/null || { echo "RESULT: REFUSED. docker pull $NEW_REF failed. Nothing was written." >&2; exit 2; }
  NEW_DIGESTS="$(digests_of "$NEW_REF")"
  case " $NEW_DIGESTS " in
    *"@$EXPECT_DIGEST "*) echo ">> gate 1 OK: $NEW_REF is $EXPECT_DIGEST" ;;
    *)
      echo "RESULT: REFUSED. $NEW_REF resolves to '${NEW_DIGESTS:-<none>}', not $EXPECT_DIGEST." >&2
      echo "  That is not the image the QA gate ran on. Nothing was written." >&2
      exit 2 ;;
  esac

  # --- gate 2: the image carries the addons, where ADDONS_PATH will point ------
  MISSING=""
  for A in $REQUIRE_ADDONS; do
    if docker run --rm --entrypoint test "$NEW_REF" -f "$TP_DIR/$A/__manifest__.py" >/dev/null 2>&1; then
      echo ">> gate 2 OK: $A at $TP_DIR/$A"
    else
      MISSING="$MISSING $A"
    fi
  done
  if ! docker run --rm --entrypoint test "$NEW_REF" -d "$TP_DIR" >/dev/null 2>&1; then
    echo "RESULT: REFUSED. $NEW_REF has no $TP_DIR; odoo would not start with it on ADDONS_PATH. Nothing was written." >&2
    exit 2
  fi
  if [ -n "$MISSING" ]; then
    echo "RESULT: REFUSED. $NEW_REF lacks:$MISSING under $TP_DIR. Nothing was written." >&2
    exit 2
  fi

  # --- rollback anchor ----------------------------------------------------------
  ROLLBACK_TAG=""
  if [ -n "$CUR_ID" ]; then
    ROLLBACK_TAG="rollback-$STAMP"
    docker tag "$CUR_ID" "$IMAGE_REPO:$ROLLBACK_TAG"
    echo ">> rollback image: $IMAGE_REPO:$ROLLBACK_TAG (= $CUR_ID)"
  else
    echo ">> WARNING: no running odoo container, so no rollback image was tagged" >&2
  fi
  cp -p .env ".env.bak.$STAMP"
  echo ">> PITR restore point for Managed PG, if the DB must go back: $STAMP (UTC, before any change)"

  rollback() {
    if [ -n "$KEEP_ON_FAILURE" ]; then
      echo ">> KEEP_ON_FAILURE set: leaving the new image and .env in place" >&2
      return 0
    fi
    echo ">> ROLLING BACK to the previous image" >&2
    cp -p ".env.bak.$STAMP" .env
    if [ -n "$ROLLBACK_TAG" ]; then
      ROLLBACK_TAG="$ROLLBACK_TAG" python3 - .env <<'PY'
import os, re, sys
path = sys.argv[1]; tag = os.environ["ROLLBACK_TAG"]
text = open(path).read()
if re.search(r"^ODOO_TAG=.*$", text, re.M):
    text = re.sub(r"^ODOO_TAG=.*$", "ODOO_TAG=" + tag, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + "\nODOO_TAG=" + tag + "\n"
open(path, "w").write(text)
PY
    fi
    dc up -d --force-recreate --no-deps odoo >&2 || true
    if wait_healthy >&2; then
      echo ">> rolled back: odoo serves on the previous image (ODOO_TAG=${ROLLBACK_TAG:-<restored .env>})" >&2
    else
      echo ">> ROLLBACK DID NOT COME UP HEALTHY -- box needs hands now" >&2
    fi
  }

  # --- edit .env ----------------------------------------------------------------
  IMAGE_TAG="$IMAGE_TAG" TP_DIR="$TP_DIR" python3 - .env <<'PY' || { cp -p ".env.bak.$STAMP" .env; echo "ERROR: .env edit failed; restored" >&2; exit 10; }
import os, re, sys
path = sys.argv[1]; tag = os.environ["IMAGE_TAG"]; tp = os.environ["TP_DIR"]
lines = open(path).read().splitlines(keepends=True)
seen_tag = seen_path = False
for i, l in enumerate(lines):
    if re.match(r"^ODOO_TAG=", l):
        lines[i] = "ODOO_TAG=" + tag + "\n"; seen_tag = True
    m = re.match(r"^ADDONS_PATH=(\S*)\s*$", l)
    if m:
        parts = [p for p in m.group(1).split(",") if p]
        if tp not in parts:
            # after the community addons, before the git-synced grove modules
            at = 1 if parts and parts[0].endswith("/odoo/addons") else len(parts)
            parts.insert(at, tp)
        lines[i] = "ADDONS_PATH=" + ",".join(parts) + "\n"; seen_path = True
        print("   .env: ADDONS_PATH -> " + ",".join(parts))
if not seen_path:
    raise SystemExit(".env has no ADDONS_PATH line; refusing to guess one")
if not seen_tag:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append("ODOO_TAG=" + tag + "\n")
print("   .env: ODOO_TAG -> " + tag)
open(path, "w").writelines(lines)
PY
  if ! dc config -q >/dev/null 2>&1; then
    cp -p ".env.bak.$STAMP" .env
    echo "ERROR: 'docker compose config' rejected the edited .env; restored" >&2; exit 10
  fi

  echo
  echo "Rollback by hand, any later time:"
  echo "  cd $DEPLOY_DIR && cp -p .env.bak.$STAMP .env \\"
  [ -n "$ROLLBACK_TAG" ] && echo "    && sed -i 's|^ODOO_TAG=.*|ODOO_TAG=$ROLLBACK_TAG|' .env \\"
  echo "    && docker compose --env-file $DEPLOY_DIR/.env up -d --force-recreate --no-deps odoo"
  echo

  # --- recreate on the new image ---------------------------------------------------
  echo ">> recreating odoo on $NEW_REF (same modules revision => entrypoint skips its own upgrade)"
  dc up -d --force-recreate --no-deps odoo
  if ! wait_healthy; then
    rollback
    echo "RESULT: FAILED. odoo did not come up on the new image." >&2; exit 10
  fi

  # --- core upgrade pass -------------------------------------------------------------
  if [ -n "$SKIP_UPGRADE" ]; then
    echo ">> SKIP_UPGRADE set: no -u pass. Core module data stays at the old image's versions until one runs."
  else
    DB_NAME_R="$(env_get DB_NAME)"
    [ -n "$DB_NAME_R" ] || { rollback; echo "RESULT: FAILED. DB_NAME missing from .env." >&2; exit 10; }
    LOG="$DEPLOY_DIR/odoo-image-upgrade.$STAMP.log"
    echo ">> running odoo -u $UPGRADE_MODULES --stop-after-init on $DB_NAME_R (log: $LOG)"
    RC=0
    dc exec -T odoo odoo -d "$DB_NAME_R" -u "$UPGRADE_MODULES" --stop-after-init --no-http --workers=0 >"$LOG" 2>&1 || RC=$?
    ERRS="$(grep -cE ' ERROR ' "$LOG" || true)"
    CRITS="$(grep -cE ' CRITICAL ' "$LOG" || true)"
    WARNS="$(grep -cE ' WARNING ' "$LOG" || true)"
    echo ">> upgrade exit $RC -- $CRITS CRITICAL, $ERRS ERROR, $WARNS WARNING lines"
    grep -E ' (ERROR|CRITICAL) ' "$LOG" | head -40 | sed 's/^/   | /' || true
    grep -E 'Modules loaded|modules loaded in' "$LOG" | tail -2 | sed 's/^/   | /' || true
    if [ "$RC" -ne 0 ] || [ "$CRITS" -gt 0 ] || [ "$ERRS" -gt "$MAX_ERROR_LINES" ]; then
      rollback
      echo "RESULT: FAILED. The -u $UPGRADE_MODULES pass was not clean (exit $RC, $CRITS CRITICAL, $ERRS ERROR; allowed ERROR lines: $MAX_ERROR_LINES)." >&2
      echo "  Log kept at $LOG. Some modules may already be upgraded in the DB; PITR point: $STAMP UTC." >&2
      exit 10
    fi
    echo ">> restarting odoo so the workers load the upgraded registry"
    dc restart odoo
    if ! wait_healthy; then
      rollback
      echo "RESULT: FAILED. odoo did not come back after the upgrade pass. Log: $LOG" >&2; exit 10
    fi
  fi

  # --- verify ---------------------------------------------------------------------------
  NOW_ID="$(running_image_id)"
  NOW_DIGESTS="$(digests_of "$NOW_ID")"
  NOW_PATH="$(dc exec -T odoo sh -c 'sed -n "s/^addons_path *= *//p" "${ODOO_RC:-/etc/odoo/odoo.conf}"' 2>/dev/null | head -1 | tr -d '\r' || true)"
  echo ">> running digests   = ${NOW_DIGESTS:-<none>}"
  echo ">> odoo addons_path  = ${NOW_PATH:-<unresolved>}"
  echo ">> running odoo      = $(dc exec -T odoo odoo --version 2>/dev/null | tr -d '\r' | head -1 || true)"
  case " $NOW_DIGESTS " in
    *"@$EXPECT_DIGEST "*) ;;
    *) echo "RESULT: FAILED verification -- the odoo container does not run $EXPECT_DIGEST; left in place for inspection" >&2; exit 10 ;;
  esac
  case ",$NOW_PATH," in
    *,"$TP_DIR",*) ;;
    *) echo "RESULT: FAILED verification -- rendered addons_path lacks $TP_DIR; left in place for inspection" >&2; exit 10 ;;
  esac
  echo "RESULT: applied and verified. odoo runs $NEW_REF ($EXPECT_DIGEST) with third-party-addons on the path."
}
main </dev/null
REMOTE
} | ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$PROD_HOST" 'bash -s'
