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
#          OP_DO_TOKEN_REF=op://...     override the vault ref for the DO token
#          DOCTL_CONFIG_PATHS=a:b       override the doctl config search path
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

# Token resolution (GOL-3061). Candidate credentials are tried IN ORDER against
# the live API and the first one that actually AUTHENTICATES wins:
#   1. $DIGITALOCEAN_TOKEN / $DIGITALOCEAN_ACCESS_TOKEN -- explicit override
#   2. $TF_VAR_do_token -- already in the environment whenever this runs inside
#      the same `op run --env-file=<qa env>/.env.op` wrapper `make qa-l3-up` uses
#   3. a local doctl config ($DOCTL_CONFIG_PATHS)
#   4. `op read` of the canonical Grove Infra `do_token` -- the SAME vault field
#      .env.op resolves, so a host with `op` needs no local DO credential at all
#
# WHY TRY-UNTIL-200 AND NOT FIRST-NON-EMPTY: a doctl config that is PRESENT but
# STALE used to end the run at `exit 2` on its HTTP 401 -- and because `train-up`
# depends on `train-preflight`, that took `make train-up` down with it and the
# train window stalled on an expired credential nobody was deliberately using
# (reproduced on the agent host 2026-10-05: ~/.config/doctl/config.yaml 401s
# while the vault `do_token` the apply itself uses is healthy). A dead token has
# to fall through to the next source, never gate the train.
OP_DO_TOKEN_REF="${OP_DO_TOKEN_REF:-op://Goldberry Grove - Admin/qvkpvg24x2wbsn6owjyvn4vhx4/rlnkse5k3p34nwamap5qhquxom}"
# `:-` would make an explicitly EMPTY value mean "use the defaults"; `-` keeps
# DOCTL_CONFIG_PATHS="" expressible as "this host has no doctl config".
DOCTL_CONFIG_PATHS="${DOCTL_CONFIG_PATHS-${HOME}/.config/doctl/config.yaml:/paperclip/.config/doctl/config.yaml}"

vols_json="$(mktemp)"
trap 'rm -f "$vols_json"' EXIT

declare -a cand_names=()
declare -a cand_tokens=()
# Skips empties, and skips a token already queued -- HOME=/paperclip on the
# agent host makes two entries of $DOCTL_CONFIG_PATHS the same file, and
# re-probing an identical token just prints the same rejection twice.
add_candidate() {
  if [[ -n "${2:-}" ]]; then
    local queued
    for queued in ${cand_tokens[@]+"${cand_tokens[@]}"}; do
      [[ "$queued" == "$2" ]] && return 0
    done
    cand_names+=("$1")
    cand_tokens+=("$2")
  fi
  return 0
}

add_candidate 'DIGITALOCEAN_TOKEN'        "${DIGITALOCEAN_TOKEN:-}"
add_candidate 'DIGITALOCEAN_ACCESS_TOKEN' "${DIGITALOCEAN_ACCESS_TOKEN:-}"
add_candidate 'TF_VAR_do_token'           "${TF_VAR_do_token:-}"
while IFS= read -r cfg; do
  [[ -n "$cfg" && -r "$cfg" ]] || continue
  add_candidate "doctl:${cfg}" \
    "$(sed -n 's/^[[:space:]]*access-token:[[:space:]]*\(\S*\).*/\1/p' "$cfg" | head -1)"
done < <(tr ':' '\n' <<<"$DOCTL_CONFIG_PATHS")
# `op` is not always on PATH -- on the agent host it lives in ~/bin.
op_bin=""
for maybe_op in op "${HOME}/bin/op"; do
  if command -v "$maybe_op" >/dev/null 2>&1; then
    op_bin="$maybe_op"
    break
  fi
done
if [[ -n "$op_bin" ]]; then
  add_candidate 'op://Grove Infra do_token' \
    "$("$op_bin" read "$OP_DO_TOKEN_REF" 2>/dev/null || true)"
fi

if [[ "${#cand_tokens[@]}" -eq 0 ]]; then
  echo "  ERROR: no DigitalOcean token. Set DIGITALOCEAN_TOKEN, configure doctl," >&2
  echo "         or make 'op read \"$OP_DO_TOKEN_REF\"' resolve." >&2
  exit 2
fi

DO_TOKEN=""
DO_TOKEN_SOURCE=""
http=""
for idx in "${!cand_tokens[@]}"; do
  http="$(curl -sS -m 30 -o "$vols_json" -w '%{http_code}' \
    -H "Authorization: Bearer ${cand_tokens[$idx]}" \
    'https://api.digitalocean.com/v2/volumes?per_page=200' || echo 000)"
  if [[ "$http" == "200" ]]; then
    DO_TOKEN="${cand_tokens[$idx]}"
    DO_TOKEN_SOURCE="${cand_names[$idx]}"
    break
  fi
  warn "DO credential ${cand_names[$idx]} rejected (HTTP ${http}) -- trying the next source"
done
if [[ -z "$DO_TOKEN" ]]; then
  echo "  ERROR: every DigitalOcean credential was rejected (last HTTP ${http})." >&2
  echo "         Tried: ${cand_names[*]}" >&2
  exit 2
fi
ok "DO API authenticated via ${DO_TOKEN_SOURCE}"

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
if [[ -x "$LOCK_CHECK" || -f "$LOCK_CHECK" ]]; then
  if bash "$LOCK_CHECK"; then
    ok "state locking mutually excludes"
  else
    bad "state lock check FAILED -- concurrent applies can corrupt state"
  fi
elif [[ "$ACK_LOCK" == "1" ]]; then
  warn "locking UNVERIFIED ($LOCK_CHECK absent, odoocker #790 unmerged);"
  warn "proceeding on TRAIN_PREFLIGHT_ACK_LOCK=1 -- do NOT run a second"
  warn "apply against any Grove env until this one finishes."
else
  bad "locking UNVERIFIED: $LOCK_CHECK is absent (odoocker #790 unmerged) and"
  bad "DO Spaces does not honour If-None-Match, so use_lockfile is a NO-OP."
  bad "Re-run with TRAIN_PREFLIGHT_ACK_LOCK=1 to accept serialising by hand."
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
