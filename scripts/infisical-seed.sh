#!/usr/bin/env bash
#
# Seed an Infisical Cloud project with secrets — odoocker side.
#
# Usage:
#   op run --env-file=.env.infisical-seed.op -- ./scripts/infisical-seed.sh
#
# Reads from environment (populated by op run):
#   INFISICAL_TOKEN              — Infisical service token (Universal Auth output)
#   INFISICAL_PROJECT_ID         — slug of the target project (e.g. "grove-odoocker")
#   INFISICAL_ENV_SLUG           — environment slug (default: "prod")
#   DIGITALOCEAN_TOKEN           — value to seed
#   DISCORD_OPS_WEBHOOK_URL      — value to seed
#   SPACES_ACCESS_KEY_ID         — value to seed (from state-backend TF output)
#   SPACES_SECRET_ACCESS_KEY     — value to seed (from state-backend TF output)
#
# Idempotent: Infisical's `secrets set` upserts. Re-runs safely after rotation.
#
# Per [[project_infisical_decision_cloud]]: this is Phase 1 of the OIDC retrofit.
# Phase 2 (one-way GitHub sync) is wired in the Infisical UI, not here.

set -euo pipefail

# ── preflight ────────────────────────────────────────────────────────────────
if ! command -v infisical >/dev/null 2>&1; then
  echo "ERROR: infisical CLI not found. Install: brew install infisical/get-cli/infisical" >&2
  exit 1
fi

: "${INFISICAL_TOKEN:?must be set — typically via op run --env-file=.env.infisical-seed.op}"
: "${INFISICAL_PROJECT_ID:?must be set — the project slug (e.g. grove-odoocker)}"
INFISICAL_ENV_SLUG="${INFISICAL_ENV_SLUG:-prod}"

# Sanity-check token works before doing anything destructive.
if ! infisical secrets --projectId="$INFISICAL_PROJECT_ID" --env="$INFISICAL_ENV_SLUG" >/dev/null 2>&1; then
  echo "ERROR: infisical CLI auth failed against project=$INFISICAL_PROJECT_ID env=$INFISICAL_ENV_SLUG" >&2
  echo "  Check INFISICAL_TOKEN scope + project slug + env slug." >&2
  exit 1
fi

# ── secret name → env var name mapping ───────────────────────────────────────
# Each entry: <Infisical secret name>:<env var name read by this script>
SECRETS=(
  "DIGITALOCEAN_TOKEN:DIGITALOCEAN_TOKEN"
  "DISCORD_OPS_WEBHOOK_URL:DISCORD_OPS_WEBHOOK_URL"
  "SPACES_ACCESS_KEY_ID:SPACES_ACCESS_KEY_ID"
  "SPACES_SECRET_ACCESS_KEY:SPACES_SECRET_ACCESS_KEY"
)

echo "=== Infisical seed: project=$INFISICAL_PROJECT_ID env=$INFISICAL_ENV_SLUG ==="

missing=0
for entry in "${SECRETS[@]}"; do
  name="${entry%%:*}"
  src_var="${entry##*:}"
  value="${!src_var:-}"
  if [ -z "$value" ]; then
    echo "  ⚠  $name — source env var $src_var is empty; skipping" >&2
    missing=$((missing + 1))
    continue
  fi
  # `infisical secrets set` upserts. --silent so the value doesn't echo.
  if infisical secrets set "$name=$value" \
        --projectId="$INFISICAL_PROJECT_ID" \
        --env="$INFISICAL_ENV_SLUG" \
        --silent >/dev/null 2>&1; then
    echo "  ✓  $name"
  else
    echo "  ✗  $name — set failed (check infisical CLI version + token scope)" >&2
    exit 1
  fi
done

if [ "$missing" -gt 0 ]; then
  echo "" >&2
  echo "WARN: $missing secret(s) skipped due to empty source vars." >&2
  echo "  This is expected for SPACES_* if state-backend hasn't been applied yet —" >&2
  echo "  fetch via: cd infra/terraform/environments/state-backend && terraform output -raw spaces_access_key_id" >&2
fi

echo ""
echo "Seed complete. Verify in UI:"
echo "  https://app.infisical.com/organization/952236a8-4ed4-45c0-81e8-5157b48557a2/secret-manager/$INFISICAL_PROJECT_ID/secrets/$INFISICAL_ENV_SLUG"
