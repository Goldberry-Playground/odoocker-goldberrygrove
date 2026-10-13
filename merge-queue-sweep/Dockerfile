# Merge-queue arming sweep runner (GOL-3159): supercronic (scheduler) + the
# agent-plane arming sweep. Same shape as cost/ and do-metrics/, with one
# important difference -- this one runs on the AGENT PLANE, beside
# gh-token-broker, not on the observability droplet. See README.md for why.
#
# Base is node, not python, because the sweep needs BOTH:
#   node    -- scripts/ci/protected-paths-carveout.mjs, fetched at runtime from
#              each target repo's base branch and executed to decide whether an
#              unapproved PR touches a protected path. Without node the
#              protected-path check cannot run and the sweep fails closed,
#              skipping every unapproved PR -- i.e. doing nothing, quietly.
#   python3 -- the arming selector and this wrapper's streak arithmetic.
#
# Build context is the REPO ROOT (not ./merge-queue-sweep) because the image
# needs scripts/ci/. One copy of the arming script serves all three repos: it
# already takes REPO= and reads each target's own carve-out from that repo's
# base branch at runtime.
#
#   docker build -f merge-queue-sweep/Dockerfile -t grove/merge-queue-sweep .
FROM node:22-bookworm-slim

ARG SUPERCRONIC_VERSION=v0.2.33

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates python3; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sc_arch=amd64 ;; \
      arm64) sc_arch=arm64 ;; \
      *) sc_arch="$arch" ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${sc_arch}"; \
    chmod +x /usr/local/bin/supercronic; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scripts/ci/merge-queue-arm-automerge.sh /app/scripts/ci/
COPY scripts/ci/merge-queue-arm-sweep.sh     /app/scripts/ci/
COPY merge-queue-sweep/crontab               /app/crontab
RUN chmod +x /app/scripts/ci/*.sh

# The sweep writes its consecutive-failure streaks here. Mount a volume to keep
# streaks across restarts; without one, a container restart resets the streak
# and the alert threshold starts over (safe, just slower to page).
ENV SWEEP_STATE_DIR=/var/lib/merge-queue-sweep
RUN mkdir -p /var/lib/merge-queue-sweep

CMD ["supercronic", "/app/crontab"]
