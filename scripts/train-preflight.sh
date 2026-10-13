#!/usr/bin/env bash
# train-preflight.sh -- Assert the Release Train's pre-window invariants BEFORE
# `make train-up` runs a terraform apply.
#
# WHY THIS EXISTS (GOL-2584, 2026-09-30):
# Every train so far has re-verified the same handful of prerequisites BY HAND,
# out of the manifest issue, and the answers have drifted between the manifest
# and reality more than once. Train #2's manifest, for example, still listed the
# GOL-2436 durable-volume program as "QA-only since Train #1" -- the live DO API
# says both PROD volumes have been attached since 2026-07-07 / 2026-07-23. A
# hand-audited prerequisite list that nobody can re-run is not a gate.
#
# The checks below are the ones that are (a) mechanically decidable from outside
# the box and (b) capable of eating a train window or prod data if they are
# wrong. Everything read-only; no SSH, no terraform state mutation.
#
#   [1] durable volumes -- the QA filestore/caddy volumes must SURVIVE a
#       `train-teardown compute`, and the prod filestore/blogs volumes must be
#       attached. `qa-l3-teardown.sh compute` targets the volume ATTACHMENTS,
#       not the volumes, and both carry prevent_destroy (#237) -- but that is an
#       intent expressed in code, and this asserts the intent against the live
#       DO API. A QA volume that actually got destroyed comes back EMPTY at the
#       next train-up and the window is spent rebuilding fixtures.
#
#   [2] terraform state locking -- `train-up` is a terraform apply. On
#       2026-09-29 two concurrent QA teardown runs BOTH acquired the "lock" and
#       the second 404'd releasing it. Root cause (GOL-2755): terraform's
#       S3-native `use_lockfile` rests entirely on the backend rejecting
#       PUT <key>.tflock with `If-None-Match: *` once the object exists, and DO
#       Spaces answers 200 instead of 412. There is therefore ZERO mutual
#       exclusion in all nine environments, production included. This check
#       delegates to scripts/tf-state-lock-check.sh; while that is unmerged the
#       check reports UNVERIFIED and requires a deliberate ack to proceed.
#
# Usage:   bash scripts/train-preflight.sh
# Escapes: TRAIN_PREFLIGHT_ACK_LOCK=1  proceed with state locking UNVERIFIED
#                                      (the standing Train #2 posture -- the
#                                      human running train-up IS the lock)
#          TRAIN_PREFLIGHT_WARN_ONLY=1 report but never fail (read-only callers)
# Exit:    0 = clear to run train-up   1 = a blocking invariant failed
#          2 = usage / missing credential
set -euo pipefail

WARN_ONLY="${TRAIN_PREFLIGHT_WARN_ONLY:-0}"
ACK_LOCK="${TRAIN_PREFLIGHT_ACK_LOCK:-0}"
LOCK_CHECK="scripts/tf-state-lock-check.sh"

fail=0
ok()    { printf '  OK    %s\n' "$1"; }
warn()  { printf '  WARN  %s\n' "$1"; }
bad()   { printf '  FAIL  %s\n' "$1"; fail=1; }

# Volumes that must exist. "attached" = must currently be attached to a droplet;
# QA volumes are legitimately detached between train windows (that is the whole
# point of a compute-only teardown), so they are only required to EXIST.
#   name<TAB>attached|detached-ok<TAB>why
read -r -d '' EXPECTED <<'EOF' || true
nyc3-grove-prod-odoo-filestore	attached	prod /var/lib/odoo -- every product photo + ir.attachment binary (GOL-93)
nyc3-grove-prod-blogs-data	attached	prod MySQL + 4 Ghost content dirs
nyc3-grove-qa-l3-odoo-filestore	detached-ok	QA filestore -- must survive train-teardown
nyc3-grove-qa-l3-caddy-data	detached-ok	QA Let's Encrypt certs -- destroying this burns LE rate limit
EOF

echo "Release Train preflight (GOL-2584)"
echo
echo "[1] Durable volumes (GOL-2436 / GOL-93 / #237)"

# Token: prefer an explicit env var, else doctl's DEFAULT-context token.
# doctl writes its config to $XDG_CONFIG_HOME/doctl (Linux, the agent plane) or
# ~/Library/Application Support/doctl (macOS, where Josh runs train-up); the
# macOS path was missing, so a configured doctl read as "no token" (10-05).
# Only the column-0 `access-token:` line is the default context -- indented
# ones belong to other, possibly stale, contexts. `[^...]*` not `\S`: BSD sed
# (macOS) has no `\S` and silently matched nothing.
DO_TOKEN="${DIGITALOCEAN_TOKEN:-${DIGITALOCEAN_ACCESS_TOKEN:-}}"
if [[ -z "$DO_TOKEN" ]]; then
  for cfg in "${XDG_CONFIG_HOME:-${HOME}/.config}/doctl/config.yaml" \
             "${HOME}/Library/Application Support/doctl/config.yaml" \
             "/paperclip/.config/doctl/config.yaml"; do
    if [[ -r "$cfg" ]]; then
      DO_TOKEN="$(sed -n "s/^access-token:[[:space:]]*[\"']\{0,1\}\([^\"'[:space:]]*\).*/\1/p" "$cfg" | head -1)"
      [[ -n "$DO_TOKEN" ]] && break
    fi
  done
fi
if [[ -z "$DO_TOKEN" ]]; then
  echo "  ERROR: no DigitalOcean token (set DIGITALOCEAN_TOKEN or configure doctl)." >&2
  exit 2
fi

vols_json="$(mktemp)"
trap 'rm -f "$vols_json"' EXIT
http="$(curl -sS -m 30 -o "$vols_json" -w '%{http_code}' \
  -H "Authorization: Bearer ${DO_TOKEN}" \
  "${TRAIN_PREFLIGHT_DO_API:-https://api.digitalocean.com}/v2/volumes?per_page=200")"
if [[ "$http" != "200" ]]; then
  echo "  ERROR: DO volumes API returned HTTP ${http}." >&2
  exit 2
fi

# Emit "name<TAB>count-of-attached-droplets" for every volume in the account.
live="$(python3 -c '
import json,sys
for v in json.load(open(sys.argv[1]))["volumes"]:
    print(v["name"], len(v.get("droplet_ids") or []), sep="\t")
' "$vols_json")"

while IFS=$'\t' read -r name mode why; do
  [[ -z "$name" ]] && continue
  n_att="$(awk -F'\t' -v n="$name" '$1==n{print $2}' <<<"$live")"
  if [[ -z "$n_att" ]]; then
    bad "$name MISSING from the DO account -- $why"
  elif [[ "$mode" == "attached" && "$n_att" == "0" ]]; then
    bad "$name exists but is NOT attached -- $why"
  elif [[ "$mode" == "attached" ]]; then
    ok "$name attached ($why)"
  else
    ok "$name exists, ${n_att} attachment(s) -- survived teardown ($why)"
  fi
done <<<"$EXPECTED"

echo
echo "[2] Terraform state locking (GOL-2755)"
# DO Spaces does not honour If-None-Match, so `use_lockfile` is a no-op and
# `tf-state-lock-check.sh probe` FAILS by design on this backend -- running it
# here would block every train. The control that exists is the advisory
# `guard` (#790): it refuses to start while a .tflock is held, and it needs the
# state-bucket credentials that only `op run` inside qa-l3-up / the teardown
# has. So this asserts the guard is WIRED into both legs; the guard itself runs
# seconds before the apply/destroy. (Calling the script bare here, as this
# section first did, always exited 2 = FAIL once #790 landed.)
if [[ -f "$LOCK_CHECK" ]] \
   && grep -q 'tf-state-lock-check.sh guard qa-app-platform/terraform.tfstate' Makefile \
   && grep -q 'tf-state-lock-check.sh" guard qa-app-platform/terraform.tfstate' scripts/qa-l3-teardown.sh; then
  ok "held-lock guard wired into qa-l3-up and qa-l3-teardown (advisory: Spaces"
  warn "cannot enforce locks -- do NOT run a second apply against any Grove env"
  warn "until this one finishes; re-home state per ADR-011 / GOL-2760)"
elif [[ "$ACK_LOCK" == "1" ]]; then
  warn "held-lock guard NOT wired ($LOCK_CHECK or its train-up/teardown call missing);"
  warn "proceeding on TRAIN_PREFLIGHT_ACK_LOCK=1 -- do NOT run a second"
  warn "apply against any Grove env until this one finishes."
else
  bad "held-lock guard NOT wired: $LOCK_CHECK or its call in Makefile qa-l3-up /"
  bad "scripts/qa-l3-teardown.sh is missing, and DO Spaces does not honour"
  bad "If-None-Match, so use_lockfile is a NO-OP. Re-run with"
  bad "TRAIN_PREFLIGHT_ACK_LOCK=1 to accept serialising by hand."
fi

echo
if [[ "$fail" == "0" ]]; then
  echo "PREFLIGHT PASS -- clear to run make train-up."
  exit 0
fi
if [[ "$WARN_ONLY" == "1" ]]; then
  echo "PREFLIGHT FAILED (warn-only: not blocking)."
  exit 0
fi
echo "PREFLIGHT FAILED -- fix the above before make train-up." >&2
exit 1
