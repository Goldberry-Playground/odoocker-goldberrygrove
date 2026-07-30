#!/usr/bin/env bash
# check-checkout-secrets-wired.sh -- Guard against a silently-inert checkout.
#
# WHY THIS EXISTS (GOL-899, 2026-07-28..30):
# grove_headless serves the checkout SESSION route
# (/grove/api/v1/checkout/session) and reads its Stripe key from the LOWERCASE
# process env `stripe_test_secret_key`. Every storefront (nursery + ggg +
# goldberry) proxies to that one route (thin-proxy PRs #278/#279, GOL-890), so
# that ONE env var is the single point of failure for ALL checkout in QA.
#
# The regression: a prior change COMMENTED OUT `TF_VAR_stripe_test_secret_key`
# in .env.op (intending to mint a dedicated backend key later). The TF variable
# defaults to "" so `plan`/`apply` stayed green, and the code path degraded
# gracefully to a 503 "Checkout is not configured yet" -- no error, no alert.
# Checkout was dead for a week before anyone noticed. An empty default is NOT a
# safe no-op here; it is an outage that hides behind a clean plan.
#
# This guard makes that failure mode LOUD at PR time: if the checkout code path
# is wired (webhook secret is set) but the secret-key ref is missing/commented,
# CI reds. It is a static ref-presence check -- it does NOT read secret VALUES
# (no 1Password access, no network), so it is safe to run anywhere.
#
# Usage:   bash scripts/check-checkout-secrets-wired.sh
# Exit:    0 = both refs present & active   1 = a required ref is missing
set -euo pipefail

ENV_OP="infra/terraform/environments/qa-app-platform/.env.op"

fail=0
note() { printf '  %s\n' "$1"; }

if [[ ! -f "$ENV_OP" ]]; then
  echo "ERROR: $ENV_OP not found (run from repo root)." >&2
  exit 1
fi

# An "active" assignment is a line that starts the var name at column 0 (no
# leading '#'). grep -E '^VAR=' rejects the commented `#VAR=` placeholder.
check_active() {
  local var="$1" human="$2"
  if grep -Eq "^${var}=\"op://" "$ENV_OP"; then
    note "OK   ${var} -> active op:// ref"
  else
    note "FAIL ${var} is missing or commented out"
    note "     ${human}"
    fail=1
  fi
}

echo "Checkout secret wiring guard: $ENV_OP"
check_active "TF_VAR_stripe_test_secret_key" \
  "grove_headless checkout SESSION route reads lowercase env stripe_test_secret_key; empty => 503 for ALL storefronts."
check_active "TF_VAR_stripe_test_webhook_secret" \
  "Stripe -> grove_headless webhook verification; empty => payment confirmations rejected."

if [[ "$fail" -ne 0 ]]; then
  echo
  echo "Checkout is wired in code but a Stripe secret ref is not active in $ENV_OP." >&2
  echo "This means QA checkout will 503 after the next apply. See" >&2
  echo "docs/RUNBOOK-checkout-stripe-guardrails.md before merging." >&2
  exit 1
fi

echo "PASS: checkout Stripe secret refs are active."
