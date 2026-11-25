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
# PROD (GOL-3802): prod has NO global key. The misnamed global
# `stripe_test_secret_key` was retired once all three tenants had their own
# `stripe_secret_key_{tenant}`; grove_headless fails a tenant closed (503) when
# any per-tenant key is set and that tenant's is not. So in prod the same
# silent-outage shape is "one of the three per-tenant refs got dropped", and
# the retired global must not come back (one global key = one LLC's account
# for every storefront).
#
# Usage:   bash scripts/check-checkout-secrets-wired.sh
# Exit:    0 = all refs present & active   1 = a required ref is missing
set -euo pipefail

ENV_OP="${CHECKOUT_QA_ENV_OP:-infra/terraform/environments/qa-app-platform/.env.op}"
PROD_ENV_OP="${CHECKOUT_PROD_ENV_OP:-infra/terraform/environments/production/.env.op}"

fail=0
note() { printf '  %s\n' "$1"; }

for f in "$ENV_OP" "$PROD_ENV_OP"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: $f not found (run from repo root)." >&2
    exit 1
  fi
done

# An "active" assignment is a line that starts the var name at column 0 (no
# leading '#'). grep -E '^VAR=' rejects the commented `#VAR=` placeholder.
check_active() {
  local var="$1" human="$2" file="${3:-$ENV_OP}"
  if grep -Eq "^${var}=\"?op://" "$file"; then
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

echo "Checkout secret wiring guard: $PROD_ENV_OP"
for tenant in goldberry nursery ggg; do
  check_active "TF_VAR_stripe_secret_key_${tenant}" \
    "prod has no global Stripe key; a missing per-tenant ref => ${tenant} checkout 503s after the next rebuild." \
    "$PROD_ENV_OP"
done
if grep -Eq '^TF_VAR_stripe_test_secret_key=' "$PROD_ENV_OP"; then
  note "FAIL TF_VAR_stripe_test_secret_key is back in $PROD_ENV_OP"
  note "     retired in GOL-3802: a global key charges every storefront into ONE LLC's account."
  fail=1
else
  note "OK   TF_VAR_stripe_test_secret_key -> absent (retired global)"
fi

if [[ "$fail" -ne 0 ]]; then
  echo
  echo "Checkout is wired in code but a Stripe secret ref is wrong in $ENV_OP or $PROD_ENV_OP." >&2
  echo "This means checkout will 503 (or misroute revenue) after the next apply. See" >&2
  echo "docs/RUNBOOK-checkout-stripe-guardrails.md before merging." >&2
  exit 1
fi

echo "PASS: checkout Stripe secret refs are active."
