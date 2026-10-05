#!/usr/bin/env bash
# Terraform state-lock preflight + backend-enforcement probe (GOL-2584).
#
# WHY THIS EXISTS
# ---------------
# Every Grove TF env sets `use_lockfile = true` in its `backend "s3"` block and
# every one of them carried the comment "verified DO Spaces enforces it (2nd
# writer gets HTTP 412)". That claim is FALSE as of 2026-09-30.
#
# Terraform's S3-native locking is built entirely on the backend rejecting a
# conditional `PUT <key>.tflock` with `If-None-Match: *` once the object exists.
# DigitalOcean Spaces (nyc3) ACCEPTS that PUT and overwrites (HTTP 200), so
# every concurrent run "acquires" the same lock and terraform provides ZERO
# mutual exclusion. Measured 2026-09-30 against grove-tf-state:
#
#     PUT _locktest/probe  If-None-Match: *   -> 200
#     PUT _locktest/probe  If-None-Match: *   -> 200   (should be 412)
#     GET _locktest/probe                     -> "second"   (overwritten)
#
# Two concurrent `terraform plan -lock-timeout=0` on cloudflare-policy both ran
# to completion; neither printed "Error acquiring the state lock", and the first
# to finish deleted the lockfile so the other died with "Error releasing the
# state lock ... StatusCode: 404". That is exactly the 2026-09-29 double-teardown
# signature.
#
# Until state locking is re-homed onto a backend that actually enforces mutual
# exclusion (decision issue: see GOL-2584 thread), `guard` below is the only
# control standing between two overlapping applies and a corrupted state file.
# It is an ADVISORY check: it closes the realistic window (a second operator
# starting minutes into someone else's run) but cannot close a sub-second race,
# because the atomic primitive it would need is the very thing Spaces lacks.
#
# USAGE
#   bash scripts/tf-state-lock-check.sh probe                  # is enforcement fixed yet?
#   bash scripts/tf-state-lock-check.sh guard <state-key>      # refuse if a lock is held
#
# Both modes need AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY for the bucket under
# test (i.e. run under `op run --env-file=<env>/.env.op`).
#
# `probe` is BACKEND-AGNOSTIC (GOL-2760). It judges whichever S3-compatible
# endpoint you point it at, so it doubles as the acceptance test for a candidate
# replacement backend -- you never have to believe a compatibility matrix:
#
#   # the incumbent (DO Spaces, nyc3) -- known FAIL, 200 instead of 412
#   op run --env-file=infra/terraform/environments/production/.env.op -- \
#     bash scripts/tf-state-lock-check.sh probe
#
#   # a candidate Cloudflare R2 bucket (ADR-011 option 5, GOL-2760).
#   # R2 issues its own S3-compatible key pair; SigV4 region must be `auto`.
#   AWS_ACCESS_KEY_ID=$(op read 'op://Goldberry Grove - Admin/Grove Infra/r2_access_key_id') \
#   AWS_SECRET_ACCESS_KEY=$(op read 'op://Goldberry Grove - Admin/Grove Infra/r2_secret_access_key') \
#   GROVE_S3_HOST=<account-id>.r2.cloudflarestorage.com \
#   GROVE_S3_REGION=auto \
#   GROVE_TF_STATE_BUCKET=grove-tf-state-probe \
#     bash scripts/tf-state-lock-check.sh probe
#
#   # a MinIO / AWS S3 candidate -- same shape, different host/region
#
# ENV
#   GROVE_TF_STATE_BUCKET   bucket to probe/guard (default: grove-tf-state)
#   GROVE_S3_HOST           S3 endpoint host     (default: nyc3.digitaloceanspaces.com)
#   GROVE_SPACES_HOST       deprecated alias for GROVE_S3_HOST, still honoured
#   GROVE_S3_REGION         SigV4 region scope   (default: us-east-1; R2 wants `auto`)
#
# `probe` is also the regression test for the incumbent: when DO ships
# conditional-write support (or state moves to a locking backend), `probe`
# starts passing and the guard can be retired. Re-run it before each
# release-train window, and against any candidate backend before migrating.
set -euo pipefail

BUCKET="${GROVE_TF_STATE_BUCKET:-grove-tf-state}"
# GROVE_S3_HOST is the canonical name (matches scripts/tf-state-inventory.sh);
# GROVE_SPACES_HOST is the pre-GOL-2760 name and stays honoured so existing
# callers and runbooks do not silently fall back to the Spaces default.
ENDPOINT_HOST="${GROVE_S3_HOST:-${GROVE_SPACES_HOST:-nyc3.digitaloceanspaces.com}}"
# SigV4 credential-scope region. Spaces and AWS want a real region; R2 wants
# `auto` and rejects the signature otherwise.
S3_REGION="${GROVE_S3_REGION:-us-east-1}"
# Test seam only (scripts/test_tf_state_lock_check.py points this at a local
# stub server); real runs always talk https. Same canonical/legacy pairing.
ENDPOINT_SCHEME="${GROVE_S3_SCHEME:-${GROVE_SPACES_SCHEME:-https}}"
MODE="${1:-}"

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "tf-state-lock-check: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY unset." >&2
  echo "  Run under: op run --env-file=infra/terraform/environments/<env>/.env.op -- $0 $*" >&2
  exit 2
fi

# Minimal SigV4 S3 client (stdlib only -- no awscli/boto on the ops hosts).
_s3() { # _s3 <METHOD> <key> [body] [cond]
  GROVE_S3_HOST="$ENDPOINT_HOST" GROVE_S3_REGION="$S3_REGION" GROVE_S3_SCHEME="$ENDPOINT_SCHEME" python3 - "$@" <<'PY'
import hashlib, hmac, os, sys, datetime, urllib.request, urllib.error
AK=os.environ["AWS_ACCESS_KEY_ID"]; SK=os.environ["AWS_SECRET_ACCESS_KEY"]
HOST=os.environ["GROVE_S3_HOST"]; REGION=os.environ["GROVE_S3_REGION"]; SCHEME=os.environ.get("GROVE_S3_SCHEME","https"); SERVICE="s3"
method, key = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else b""
cond = len(sys.argv) > 4 and sys.argv[4] == "cond"
def sign(k, m): return hmac.new(k, m.encode(), hashlib.sha256).digest()
t=datetime.datetime.now(datetime.timezone.utc)
amzdate=t.strftime('%Y%m%dT%H%M%SZ'); datestamp=t.strftime('%Y%m%d')
ph=hashlib.sha256(body).hexdigest()
h={"host":HOST,"x-amz-content-sha256":ph,"x-amz-date":amzdate}
if cond: h["if-none-match"]="*"
sh=";".join(sorted(h)); ch="".join(f"{k}:{h[k]}\n" for k in sorted(h))
cr=f"{method}\n/{key}\n\n{ch}\n{sh}\n{ph}"
scope=f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
sts=f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"+hashlib.sha256(cr.encode()).hexdigest()
k=sign(("AWS4"+SK).encode(),datestamp); k=sign(k,REGION); k=sign(k,SERVICE); k=sign(k,"aws4_request")
sig=hmac.new(k,sts.encode(),hashlib.sha256).hexdigest()
r=urllib.request.Request(f"{SCHEME}://{HOST}/{key}", data=body or None, method=method)
for kk,vv in h.items(): r.add_header(kk,vv)
r.add_header("Authorization", f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, SignedHeaders={sh}, Signature={sig}")
try:
    with urllib.request.urlopen(r, timeout=30) as resp:
        sys.stdout.write(f"{resp.status}\n"); sys.stdout.write(resp.read().decode("utf-8","replace"))
except urllib.error.HTTPError as e:
    sys.stdout.write(f"{e.code}\n"); sys.stdout.write(e.read().decode("utf-8","replace"))
except (urllib.error.URLError, OSError) as e:
    # DNS failure, refused connection, timeout: no HTTP answer at all. Report
    # status 000 so the caller's "inconclusive -> proceed" branch handles it.
    # An uncaught traceback here exits non-zero, and both release-train legs
    # run the guard under `&&` / `set -e`, so a network blip would otherwise
    # abort train-up / teardown -- the guard must never be its own outage.
    sys.stdout.write("000\n"); sys.stdout.write(f"{e}")
PY
}

case "$MODE" in
  probe)
    PROBE_KEY="$BUCKET/_locktest/lock-enforcement-probe.json"
    # Self-describing evidence: a pasted probe result has to say WHICH backend
    # it judged and WHEN, or it becomes the next unverifiable "verified DO
    # Spaces enforces it" comment (GOL-2584).
    echo "==> Probing conditional-PUT enforcement"
    echo "    endpoint : https://$ENDPOINT_HOST"
    echo "    bucket   : $BUCKET"
    echo "    region   : $S3_REGION  (SigV4 scope)"
    echo "    measured : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    _s3 DELETE "$PROBE_KEY" >/dev/null 2>&1 || true
    # Capture whole responses first: piping straight into `head -1` can SIGPIPE
    # the python writer, and `set -o pipefail` would turn that into an abort.
    R1="$(_s3 PUT "$PROBE_KEY" first cond)"; C1="$(printf '%s' "$R1" | head -1)"
    R2="$(_s3 PUT "$PROBE_KEY" second cond)"; C2="$(printf '%s' "$R2" | head -1)"
    _s3 DELETE "$PROBE_KEY" >/dev/null 2>&1 || true
    echo "    first  conditional PUT : HTTP $C1  (expect 200)"
    echo "    second conditional PUT : HTTP $C2  (expect 412)"
    if [ "$C1" != "200" ] && [ "$C1" != "201" ]; then
      # Never read a 403/404/501 on the FIRST put as "enforcement works" or as
      # "enforcement is broken" -- it means we never got to measure anything.
      echo "INCONCLUSIVE: the first conditional PUT returned HTTP $C1, so this run"
      echo "      measured nothing about locking. Check the credentials, the bucket"
      echo "      name, and GROVE_S3_REGION (R2 needs \`auto\`). Response body:"
      printf '%s\n' "$R1" | tail -n +2 | sed 's/^/        /'
      exit 3
    fi
    if [ "$C2" = "412" ]; then
      echo "PASS: this backend enforces If-None-Match -- \`use_lockfile\` gives real"
      echo "      mutual exclusion here. On the live state backend that means the"
      echo "      advisory guard can be retired (GOL-2584); on a candidate backend"
      echo "      it clears acceptance #1 for re-homing state (GOL-2755/GOL-2760)."
      exit 0
    fi
    echo "FAIL: second conditional PUT returned $C2, not 412. \`use_lockfile = true\`"
    echo "      is a NO-OP on this backend: concurrent applies are NOT serialized"
    echo "      and can corrupt state. Do not run two applies against one state key,"
    echo "      and do not migrate state here (GOL-2755)."
    exit 1
    ;;

  guard)
    KEY="${2:-}"
    [ -n "$KEY" ] || { echo "usage: $0 guard <state-key>   e.g. qa-app-platform/terraform.tfstate" >&2; exit 2; }
    LOCK_KEY="$BUCKET/$KEY.tflock"
    echo "==> Lock preflight: $KEY"
    OUT="$(_s3 GET "$LOCK_KEY")"
    CODE="$(printf '%s' "$OUT" | head -1)"
    if [ "$CODE" = "404" ]; then
      echo "    no lockfile present -- proceeding."
      exit 0
    fi
    if [ "$CODE" != "200" ]; then
      # Never fail the run on an inconclusive backend answer; the guard is
      # advisory and must not become its own outage.
      echo "    WARNING: inconclusive lock read (HTTP $CODE) -- proceeding, but"
      echo "    confirm by hand that nobody else is mid-apply."
      exit 0
    fi
    echo "!! A state lock is ALREADY HELD on $KEY:"
    printf '%s\n' "$OUT" | tail -n +2 | sed 's/^/       /'
    cat >&2 <<'MSG'
!!
!! Terraform will NOT stop you: `use_lockfile` is a no-op on this backend
!! (GOL-2584 -- re-run `probe` to confirm), so this run would silently
!! "acquire" the same lock and two concurrent applies can corrupt the state.
!!
!! If that lock is stale (the run that wrote it crashed), delete the .tflock
!! object from the grove-tf-state bucket and re-run. Otherwise WAIT for the
!! other run to finish.
!!
!! Override (you have confirmed no other run is live):  TF_LOCK_GUARD_OFF=1
MSG
    if [ "${TF_LOCK_GUARD_OFF:-0}" = "1" ]; then
      echo "    TF_LOCK_GUARD_OFF=1 -- overriding on operator assertion." >&2
      exit 0
    fi
    exit 1
    ;;

  *)
    echo "usage: $0 {probe|guard <state-key>}" >&2
    echo "  probe exit codes: 0 enforced (412) | 1 NOT enforced | 3 inconclusive" >&2
    exit 2
    ;;
esac
