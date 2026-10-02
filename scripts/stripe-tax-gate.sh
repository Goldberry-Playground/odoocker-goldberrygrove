#!/usr/bin/env bash
# stripe-tax-gate — documented entrypoint for GOL-2568 Gates 3 and 4.
#
# Decides, mechanically, whether the GROVE_STRIPE_TAX_{TENANT} cutover flag may
# flip at a promote. Josh's GOL-2584 ruling (2026-10-02): grove_headless#284
# ships INERT on Train #2 with every flag OFF, and the flip happens at the Wed
# promote ONLY if Gate 3 (rollback by flag-off) and Gate 4 (amounts asserted for
# a WV and a non-WV address) pass on QA. If either is unmet the code still ships
# inert and the flip moves to the next train — so an inconclusive run here is a
# no-flip, never a maybe. See scripts/stripe-tax-gate.py for the assertions and
# grove-odoo-modules docs/stripe-tax-cutover.md for the gate definitions.
#
# THE TWO GATES NEED OPPOSITE FLAG STATES, so this is a three-step sequence with
# one operator flip in the middle. Run it from a machine that can reach QA Odoo:
#
#   1. Flag OFF (how QA comes up — Terraform defaults the tenant set to empty):
#        bash scripts/stripe-tax-gate.sh --gate 3 --json-out /tmp/gate3.json
#   2. Flip nursery ON on the QA Odoo droplet, Path A (seconds, no rebuild).
#      Full recipe + the "a restart does NOT re-read env" footgun:
#      docs/RUNBOOK-module-upgrade.md -> "Flipping the Stripe Tax cutover flag".
#   3. Flag ON, then fold both halves into one verdict:
#        bash scripts/stripe-tax-gate.sh --gate 4 --merge /tmp/gate3.json \
#          --json-out /tmp/stripe-tax-verdict.json
#
#   Exit 0 = flip cleared.  Exit 1 = NOT cleared (ship inert).  Exit 2 = dry-run
#   preconditions unmet. Preview with --dry-run first; it creates nothing.
#
# AFTERWARDS: this gate creates real draft orders. They are booked to
# stripe-tax-gate@grove.invalid, an RFC 2606 reserved TLD, so
# `bash scripts/qa-test-data-cleanup.sh --apply` reaps every one of them with no
# false positives. Run the cleanup before recording any train verdict.
#
# CREDENTIALS — two supported sources, auto-detected (same shape as
# scripts/qa-test-data-cleanup.sh):
#
#   (a) Already-injected env: QA_ODOO_URL, QA_ODOO_API_KEY,
#       STRIPE_TEST_SECRET_KEY set in the environment -> runs with no `op`.
#   (b) 1Password `op run`: point STRIPE_TAX_GATE_ENV_OP at an env-file of
#       op:// refs. Template: scripts/stripe-tax-gate.env.op.
#
# The Stripe key MUST be a test-mode key (sk_test…) and the Odoo host MUST say
# "qa" — the script refuses otherwise, because it creates orders and QA shares
# the database name `odoo` with production.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER="$HERE/stripe-tax-gate.py"
ENV_OP="${STRIPE_TAX_GATE_ENV_OP:-}"

have_direct_creds() {
  [ -n "${QA_ODOO_URL:-}" ] && [ -n "${QA_ODOO_API_KEY:-}" ] && [ -n "${STRIPE_TEST_SECRET_KEY:-}" ]
}

if [ ! -f "$WORKER" ]; then
  echo "stripe-tax-gate: worker not found at $WORKER" >&2
  exit 2
fi

if have_direct_creds; then
  exec python3 "$WORKER" "$@"
fi

if [ -n "$ENV_OP" ]; then
  if [ ! -f "$ENV_OP" ]; then
    echo "stripe-tax-gate: STRIPE_TAX_GATE_ENV_OP=$ENV_OP does not exist" >&2
    exit 2
  fi
  if ! command -v op >/dev/null 2>&1; then
    echo "stripe-tax-gate: 1Password CLI 'op' not on PATH (needed for $ENV_OP)" >&2
    exit 2
  fi
  # `op run` resolves the op:// refs and injects them; the values never reach
  # shell scrollback or history.
  exec op run --env-file="$ENV_OP" -- python3 "$WORKER" "$@"
fi

# No self-skip, at the wrapper level too: missing credentials are an explicit
# failure with the fix spelled out, never a quiet pass.
cat >&2 <<'MSG'
stripe-tax-gate: no credentials.

  (a) export QA_ODOO_URL, QA_ODOO_API_KEY, STRIPE_TEST_SECRET_KEY, or
  (b) STRIPE_TAX_GATE_ENV_OP=scripts/stripe-tax-gate.env.op (fill in the item ids
      from the Grove QA vault first).

This gate decides whether a money-path flag may flip; it will not report a green
it did not earn.
MSG
exit 2
