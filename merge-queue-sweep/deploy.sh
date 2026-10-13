#!/usr/bin/env bash
# One-command deploy for the merge-queue arming sweep (GOL-3159).
#
# RUN THIS ON THE AGENT-PLANE HOST (the AgenticOS box). It cannot be run from
# inside an agent container -- there is no docker socket there, which is exactly
# why this step is a handoff rather than something the sweep's author did.
#
# What it does, in order, stopping at the first thing that is not true:
#   1. finds the docker network `gh-token-broker` is attached to (the one
#      manual lookup the README used to ask for)
#   2. confirms the broker key file exists
#   3. builds the image and starts the sidecar as a compose overlay
#   4. runs the sweep ONCE in dry-run mode inside the container and shows the
#      output, so the deploy is verified before anyone trusts the timer
#
# Nothing here arms a PR: step 4 is a dry run. The first real arming happens on
# the sidecar's own 5-minute tick.
#
#   merge-queue-sweep/deploy.sh <path-to-agenticos-compose.yml>
#
# Env overrides (all optional):
#   GH_BROKER_API_KEY_FILE   default /paperclip/gh-broker.key
#   AGENT_PLANE_NETWORK      skip autodetection and use this network
#   SWEEP_HEARTBEAT_URL      Healthchecks.io ping URL (detects a STOPPED sweep)
#   DISCORD_OPS_WEBHOOK_URL  ops webhook (detects a sweep that runs and fails)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY="$HERE/compose.agent-plane.yml"
BASE_COMPOSE="${1:-}"
KEY_FILE="${GH_BROKER_API_KEY_FILE:-/paperclip/gh-broker.key}"

die() { printf 'deploy: FATAL: %s\n' "$*" >&2; exit 1; }
log() { printf 'deploy: %s\n' "$*"; }

[ -n "$BASE_COMPOSE" ] || die "usage: $0 <path-to-agenticos-compose.yml>"
[ -f "$BASE_COMPOSE" ] || die "compose file not found: $BASE_COMPOSE"
[ -f "$OVERLAY" ] || die "overlay not found: $OVERLAY"
command -v docker >/dev/null 2>&1 || die "docker not on PATH -- run this on the agent-plane HOST, not in a container"

# 1. Which network can reach the broker? Autodetected rather than asked for: a
# wrong value here produces a sweep that starts fine and then fails every tick
# with an unreachable broker, which is the slowest possible way to find out.
if [ -n "${AGENT_PLANE_NETWORK:-}" ]; then
  NET="$AGENT_PLANE_NETWORK"
  log "using AGENT_PLANE_NETWORK=$NET (autodetection skipped)"
else
  NET="$(docker inspect gh-token-broker \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' 2>/dev/null \
    | sed '/^$/d' | head -1)"
  [ -n "$NET" ] || die "could not find a running container named gh-token-broker. Is the agent plane up? Override with AGENT_PLANE_NETWORK=<network>."
  log "broker network: $NET"
fi

# 2. The key the broker uses to mint installation tokens. Mounted read-only; the
# sweep never sees the App private key itself (ADR-0001).
[ -r "$KEY_FILE" ] || die "broker key not readable at $KEY_FILE (override GH_BROKER_API_KEY_FILE)"
log "broker key: $KEY_FILE"

# 3. Build + start.
log "building and starting merge-queue-sweep ..."
AGENT_PLANE_NETWORK="$NET" GH_BROKER_API_KEY_FILE="$KEY_FILE" \
  docker compose -f "$BASE_COMPOSE" -f "$OVERLAY" up -d --build merge-queue-sweep

# 4. Verify before trusting the timer. A dry run proves the broker is reachable,
# the token mints, GraphQL answers, and node can run each target repo's
# protected-paths carve-out -- all four of the things that make the sweep
# silently do nothing when they are wrong.
log "verifying (dry run -- arms nothing) ..."
if AGENT_PLANE_NETWORK="$NET" GH_BROKER_API_KEY_FILE="$KEY_FILE" \
     docker compose -f "$BASE_COMPOSE" -f "$OVERLAY" \
     exec -T merge-queue-sweep /app/scripts/ci/merge-queue-arm-sweep.sh; then
  log "OK -- sweep is deployed and verified. It will arm for real on the next */5 tick."
  log "Follow it with: docker compose -f $BASE_COMPOSE -f $OVERLAY logs -f merge-queue-sweep"
else
  die "the dry run failed -- see the output above. The container is running but the sweep cannot work yet; docs/RUNBOOK-merge-queue-enqueue-identity.md has the triage steps."
fi
