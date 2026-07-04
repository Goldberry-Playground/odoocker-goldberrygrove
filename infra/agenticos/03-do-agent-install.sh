#!/usr/bin/env bash
# GOL-53 P0.4a — Install the DigitalOcean metrics agent (do-agent) on the AgenticOS host.
#
# Why: the box has NO CPU/RAM/disk telemetry today (only a Healthchecks up/down ping).
# do-agent ships host metrics to DO Monitoring for free, which unlocks the native
# alert policies created by 04-do-alert-policies.sh. This is the fastest, obs-droplet-
# independent early-warning path (P1 adds the richer OpenObserve pipeline later).
#
# Idempotent: DO's installer is safe to re-run; we short-circuit if already active.
#
# Run ON the AgenticOS host as root:
#   sudo bash 03-do-agent-install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (sudo)." >&2
  exit 1
fi

if systemctl is-active --quiet do-agent 2>/dev/null; then
  echo "==> do-agent already active; nothing to do."
  systemctl status do-agent --no-pager --lines=3 || true
  exit 0
fi

echo "==> Installing do-agent via DigitalOcean's official installer."
# Official one-liner (pins to DO's apt/yum repo + systemd unit).
# Ref: https://docs.digitalocean.com/products/monitoring/how-to/install-agent/
curl -sSL https://repos.insights.digitalocean.com/install.sh | bash

echo "==> Verifying."
systemctl enable --now do-agent
systemctl is-active --quiet do-agent && echo "    do-agent is active." || {
  echo "    ERROR: do-agent not active after install." >&2
  systemctl status do-agent --no-pager --lines=20 || true
  exit 1
}

echo "==> Done. Metrics will appear in the DO control panel / API within ~1-2 min."
echo "    Verify from an env with a DO token:"
echo "      doctl monitoring alert list   # (after 04-do-alert-policies.sh)"

# Rollback:
#   curl -sSL https://repos.insights.digitalocean.com/uninstall.sh | bash
