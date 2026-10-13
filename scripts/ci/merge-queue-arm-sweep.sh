#!/usr/bin/env bash
# Recurring multi-repo wrapper around scripts/ci/merge-queue-arm-automerge.sh
# (GOL-3159, parent GOL-3150).
#
# WHY THIS EXISTS
#
# GOL-3150 put `merge-queue-arm-automerge.sh` in all three Goldberry repos and
# it works -- odoocker #857 enqueued as the App and merged in 52 s. But nothing
# ran it on a schedule anywhere, so the fix still depended on every agent
# remembering to arm its own PR in the same breath as opening it. GOL-3150 moved
# the chore rather than deleting it. This is the thing that runs unattended.
#
# WHAT THE CADENCE BUYS (measured 2026-10-06, GOL-3159)
#
# The sweep only helps if it fires inside the window between a PR being opened
# and `auto-approve.yml` approving it -- because approval and the (wedging)
# `GITHUB_TOKEN` enqueue are 6 SECONDS apart in practice, not minutes. So the
# budget is the whole open->enqueue window. Over the last 39 agent PRs that were
# enqueued by `github-actions[bot]` across the three repos:
#
#   min 1.6 min | p10 4.2 min | median 23 min | p90 183 min | max 582 min
#   13% of them were enqueued less than 5 minutes after being opened.
#
# Expected catch rate for a sweep at interval T (P = min(1, window/T)):
#
#   T =  1 min -> 100%      T = 30 min -> 69%
#   T =  5 min ->  96%      T = 60 min -> 49%
#   T = 10 min ->  88%      T =  4 hrs -> 20%
#   T = 15 min ->  83%
#
# Two things follow, and they are the reason this script is shaped the way it is:
#
#   1. The issue's starting guess -- "~5 min is likely ample" -- is right about
#      5 min being the knee of the curve, but NOT ample for "no agent PR is ever
#      enqueued by github-actions": odoocker #813 was enqueued 1.6 minutes after
#      it opened. No practical polling interval closes that. Polling is a
#      backstop, not a guarantee.
#   2. Nothing above 15 min is worth scheduling for correctness. If the cadence
#      has to be coarse (an executor that costs a whole agent run per fire, say),
#      the honest framing is backlog drain, not wedge prevention.
#
# The thing that would make cadence a LATENCY knob instead of a CORRECTNESS knob
# is removing the `GITHUB_TOKEN` enqueue from `auto-approve.yml` entirely, so
# that arming is the only enqueue path. Then an unarmed PR just waits for the
# next sweep instead of building a dead merge group, and even an hourly sweep is
# wedge-free. That is tracked separately -- it is a `.github/**` change in three
# repos and must not land before this sweep is actually running.
#
# WHERE IT RUNS
#
# NOT in GitHub Actions. The default `GITHUB_TOKEN` is the identity that causes
# the wedge, so a sweep running there would arm auto-merge as `github-actions`
# and rebuild the same dead group it exists to prevent. It needs the agent plane,
# beside `gh-token-broker` -- see merge-queue-sweep/README.md.
#
# ALERTING -- two different failures, two different channels
#
# A sweep that silently stops is worse than no sweep, and that is a DIFFERENT
# failure from a sweep that runs and errors. Neither channel detects the other's
# failure, so both are wired:
#
#   SWEEP_HEARTBEAT_URL  pinged on every completed run (Healthchecks.io style).
#                        Detects "stopped running at all" -- the case a Discord
#                        alert can never catch, because a dead sweep sends
#                        nothing. HC.io's grace period IS the "visible within
#                        one cadence" guarantee.
#   DISCORD_OPS_WEBHOOK_URL  posted when a repo fails HARD for
#                        SWEEP_FAIL_THRESHOLD consecutive runs, and again once
#                        when it recovers. Detects "runs but cannot work".
#
# HARD vs SOFT failure is the load-bearing distinction for not crying wolf:
#
#   HARD  the sweep could not EVALUATE the repo -- broker unreachable, token
#         mint failed, GraphQL returned no repository. Not self-healing; every
#         subsequent run fails the same way until someone looks. Pages.
#   SOFT  the repo was evaluated and GitHub refused one individual arm (stale
#         head, queue race, clean-status quirk). The next run re-reads the PR
#         and tries again -- the inner script is idempotent, so this heals
#         itself. Logged, never paged.
#
# Treating those the same is how an alert channel becomes noise and then becomes
# ignored, which is the same outcome as having no alert.
#
#   Dry run (default):  scripts/ci/merge-queue-arm-sweep.sh
#   Apply:              scripts/ci/merge-queue-arm-sweep.sh --apply
#   One repo:           SWEEP_REPOS=Goldberry-Playground/grove-sites ... --apply
#
# Env:
#   SWEEP_REPOS            space-separated owner/name list (default: all three)
#   SWEEP_ARM_SCRIPT       path to merge-queue-arm-automerge.sh
#                          (default: alongside this script)
#   SWEEP_STATE_DIR        consecutive-failure state (default /var/lib/merge-queue-sweep,
#                          falls back to $TMPDIR when unwritable)
#   SWEEP_FAIL_THRESHOLD   consecutive HARD failures before alerting (default 3)
#   SWEEP_FAIL_REPEAT      re-alert every Nth further consecutive failure (default 12)
#   SWEEP_HEARTBEAT_URL    dead-man's-switch ping URL (optional)
#   DISCORD_OPS_WEBHOOK_URL  ops webhook (optional)
#   ARM_PROTECTED          left UNSET deliberately -- board-gated, see the inner script

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARM_SCRIPT="${SWEEP_ARM_SCRIPT:-$HERE/merge-queue-arm-automerge.sh}"
REPOS="${SWEEP_REPOS:-Goldberry-Playground/odoocker-goldberrygrove Goldberry-Playground/grove-odoo-modules Goldberry-Playground/grove-sites}"
STATE_DIR="${SWEEP_STATE_DIR:-/var/lib/merge-queue-sweep}"
FAIL_THRESHOLD="${SWEEP_FAIL_THRESHOLD:-3}"
FAIL_REPEAT="${SWEEP_FAIL_REPEAT:-12}"

APPLY=0
ARM_ARGS=()
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
  ARM_ARGS=(--apply)
fi

log() { printf '%s sweep: %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

if [ ! -r "$ARM_SCRIPT" ]; then
  log "FATAL: arming script not readable at $ARM_SCRIPT"
  exit 1
fi

if ! mkdir -p "$STATE_DIR" 2>/dev/null || [ ! -w "$STATE_DIR" ]; then
  # A missing state dir must not stop the sweep -- arming is the job, streak
  # tracking is the telemetry. Degrade to a tmp path and say so, rather than
  # skipping the useful work over a bookkeeping problem.
  log "WARN state dir $STATE_DIR unusable; falling back to ${TMPDIR:-/tmp}"
  STATE_DIR="${TMPDIR:-/tmp}"
fi
STATE_FILE="$STATE_DIR/fail-streaks.json"

# ── Run every repo. One repo's failure must never skip the others: a broker
# scope problem on one repo is exactly when the other two still need sweeping.
HARD_FAILED=""
SOFT_FAILED=""
OK_REPOS=""
ARMED_TOTAL=0

for repo in $REPOS; do
  log "--- $repo"
  out=""
  rc=0
  out="$(env ARM_UNAPPROVED=1 REPO="$repo" bash "$ARM_SCRIPT" "${ARM_ARGS[@]+"${ARM_ARGS[@]}"}" 2>&1)" || rc=$?
  printf '%s\n' "$out" | sed 's/^/    /'

  armed="$(printf '%s\n' "$out" | grep -c '^[0-9:]*Z armed #' || true)"
  ARMED_TOTAL=$((ARMED_TOTAL + armed))

  # Classify. "FATAL:" is the inner script's own word for "could not evaluate",
  # and an absent banner line means it died before it even started -- both hard.
  if printf '%s\n' "$out" | grep -q 'FATAL:'; then
    log "HARD failure on $repo (could not evaluate)"
    HARD_FAILED="$HARD_FAILED $repo"
  elif ! printf '%s\n' "$out" | grep -q " repo=$repo "; then
    log "HARD failure on $repo (no evaluation banner; rc=$rc)"
    HARD_FAILED="$HARD_FAILED $repo"
  elif [ "$rc" -ne 0 ]; then
    log "soft failure on $repo (evaluated; an individual arm was refused, rc=$rc) -- retried next run"
    SOFT_FAILED="$SOFT_FAILED $repo"
    OK_REPOS="$OK_REPOS $repo"
  else
    OK_REPOS="$OK_REPOS $repo"
  fi
done

# ── Consecutive-HARD-failure streaks, and which transitions deserve a post.
# Done in python3 (already a dependency of the inner script) so the state file
# stays readable and the arithmetic is not shell.
ALERTS="$(env STATE_FILE="$STATE_FILE" HARD_FAILED="$HARD_FAILED" OK_REPOS="$OK_REPOS" \
  FAIL_THRESHOLD="$FAIL_THRESHOLD" FAIL_REPEAT="$FAIL_REPEAT" python3 <<'PYEOF'
import json, os

path = os.environ["STATE_FILE"]
threshold = max(1, int(os.environ.get("FAIL_THRESHOLD") or 3))
repeat = max(1, int(os.environ.get("FAIL_REPEAT") or 12))
hard = os.environ.get("HARD_FAILED", "").split()
ok = os.environ.get("OK_REPOS", "").split()

try:
    with open(path) as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        raise ValueError("state file is not an object")
except Exception:
    # A corrupt or absent state file must not suppress arming, and must not
    # fabricate a streak either. Start clean; the next real failure re-arms the
    # count, one cadence later than it could have. Losing one cadence of alert
    # latency beats either crashing the sweep or paging on invented history.
    state = {}

for repo in ok:
    prev = int(state.get(repo, 0) or 0)
    if prev >= threshold:
        print("RECOVERED\t%s\tafter %d consecutive hard failures" % (repo, prev))
    state[repo] = 0

for repo in hard:
    n = int(state.get(repo, 0) or 0) + 1
    state[repo] = n
    # Post on the exact crossing, then only every `repeat`th run after it. A
    # permanently broken broker must not post every cadence forever -- that is
    # how an ops channel gets muted.
    if n == threshold or (n > threshold and (n - threshold) % repeat == 0):
        print("FAILING\t%s\t%d consecutive hard failures" % (repo, n))

try:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
except Exception as exc:
    print("STATEERR\t%s\t%s" % (type(exc).__name__, exc))
PYEOF
)"

# ── Discord. Its own step, after everything else, and never fatal: an alerting
# failure must not turn a healthy sweep into a failed one.
#
# `DISCORD_OPS_WEBHOOK_URL` is live in the agent environment, so this posts FOR
# REAL the moment it is set. Tests must run with `env -u DISCORD_OPS_WEBHOOK_URL`.
post_discord() {
  [ -n "${DISCORD_OPS_WEBHOOK_URL:-}" ] || { log "no DISCORD_OPS_WEBHOOK_URL; alert not posted: $1"; return 0; }
  local payload
  payload="$(env MSG="$1" python3 -c 'import json,os; print(json.dumps({"content": os.environ["MSG"][:1900]}))')"
  if curl -fsS -m 20 -H 'Content-Type: application/json' -d "$payload" \
       "$DISCORD_OPS_WEBHOOK_URL" >/dev/null 2>&1; then
    log "alert posted to Discord"
  else
    log "WARN could not post to Discord"
  fi
}

while IFS=$'\t' read -r kind repo detail; do
  [ -n "${kind:-}" ] || continue
  case "$kind" in
    FAILING)
      post_discord ":rotating_light: **merge-queue arm sweep failing** — \`$repo\`: $detail. Agent PRs there will keep being enqueued by \`github-actions\` and building dead merge groups. Check \`gh-token-broker\` and the broker key, then \`scripts/ci/merge-queue-arm-sweep.sh\` by hand. Runbook: docs/RUNBOOK-merge-queue-enqueue-identity.md" ;;
    RECOVERED)
      post_discord ":white_check_mark: **merge-queue arm sweep recovered** — \`$repo\` is arming again $detail." ;;
    STATEERR)
      log "WARN could not persist failure state ($repo: $detail)" ;;
  esac
done <<< "$ALERTS"

# ── Dead-man's switch. Pinged whether or not individual repos failed: this
# signal answers "is the sweep alive", and the Discord path above answers "is it
# working". Conflating them would mean a broken-but-running sweep also looks
# dead, and a dead sweep would be indistinguishable from a healthy quiet one.
if [ -n "${SWEEP_HEARTBEAT_URL:-}" ]; then
  if curl -fsS -m 20 "$SWEEP_HEARTBEAT_URL" >/dev/null 2>&1; then
    log "heartbeat pinged"
  else
    log "WARN heartbeat ping failed"
  fi
fi

log "done: armed=$ARMED_TOTAL apply=$APPLY hard_failed=[${HARD_FAILED# }] soft_failed=[${SOFT_FAILED# }]"

# Exit 0 on a soft failure: the run did its job and the refusal retries itself
# next cadence. Non-zero only when a repo could not be evaluated at all, so a
# supervisor's own restart/alert logic keys on the same thing the streak does.
[ -z "$HARD_FAILED" ]
