#!/usr/bin/env bash
# check-publish-webhook-secrets-wired.sh -- Refuse an apply that would ZERO a
# live publish-webhook HMAC secret.
#
# WHY THIS EXISTS (GOL-2518, 2026-09-23):
# The QA publish-webhook secrets (Odoo sender <-> storefront receiver,
# GOL-985/986/1004, and the GOL-1896 sellout fast path) have no durable home.
# `variables.tf` defaults grove_publish_webhook_secret_<tenant> to "" and the
# three .env.op refs are COMMENTED OUT because the 1Password `Grove QA` items
# were never created (the ops service account is read-only there). So every
# apply -- and every release-train `make qa-l3-up` -- silently overwrites
# whatever was provisioned live with "".
#
# That already happened once: goldberry was live-provisioned 2026-07-30 and by
# 2026-09-23 BOTH halves read empty (droplet /etc/grove/.env
# GROVE_PUBLISH_WEBHOOK_SECRET_GOLDBERRY length 0, and grove-goldberry-qa's
# GROVE_PUBLISH_WEBHOOK_SECRET length 0). Nobody noticed, because a missing
# secret fails SILENTLY: the Odoo emit raises before any grove.publish.event
# row is written, and the receiver just 401s. Nursery was live-provisioned
# 2026-09-23 (GOL-2337) and would regress the same way at the next train.
#
# The rule "do NOT run qa-l3-up before the 1Password items exist" was a comment
# a human had to remember. This guard makes it an enforced gate.
#
# Two modes, picked automatically:
#   runtime -- if TF_VAR_grove_publish_webhook_secret_<tenant> is present in the
#              environment (i.e. we are inside `op run`), require it NON-EMPTY.
#              This catches a ref that resolves to an empty vault field, which
#              the static check cannot see.
#   static  -- otherwise, require an ACTIVE (uncommented) op:// ref for it in
#              .env.op. No 1Password access, no network: safe to run anywhere.
#
# Secret VALUES are never read, printed, or compared -- only "set and non-empty".
#
# Usage:   bash scripts/check-publish-webhook-secrets-wired.sh
# Escape:  ALLOW_EMPTY_PUBLISH_SECRETS=1   deliberate apply with empty secrets
#          PUBLISH_SECRET_GUARD_WARN_ONLY=1  report but never fail (used by
#                                            qa-l3-plan, which is read-only)
# Exit:    0 = all three wired (or overridden)   1 = at least one would be zeroed
set -euo pipefail

ENV_OP="infra/terraform/environments/qa-app-platform/.env.op"
TENANTS=(goldberry ggg nursery)

note() { printf '  %s\n' "$1"; }

if [[ ! -f "$ENV_OP" ]]; then
  echo "ERROR: $ENV_OP not found (run from repo root)." >&2
  exit 1
fi

echo "Publish-webhook secret guard (GOL-2518): $ENV_OP"

fail=0
for tenant in "${TENANTS[@]}"; do
  var="TF_VAR_grove_publish_webhook_secret_${tenant}"
  if [[ -n "${!var+set}" ]]; then
    # Runtime mode: the variable reached us, so op run resolved it.
    if [[ -n "${!var}" ]]; then
      note "OK   ${var} -> resolved, non-empty"
    else
      note "FAIL ${var} resolved EMPTY (vault field blank?)"
      note "     apply would set grove-${tenant}-qa GROVE_PUBLISH_WEBHOOK_SECRET=\"\""
      fail=1
    fi
  elif grep -Eq "^${var}=\"?op://" "$ENV_OP"; then
    # Active ref but the var did not reach us: we are outside `op run`.
    note "OK   ${var} -> active op:// ref (not resolved here)"
  else
    note "FAIL ${var} is missing or commented out in .env.op"
    note "     apply would set grove-${tenant}-qa GROVE_PUBLISH_WEBHOOK_SECRET=\"\""
    note "     and drop GROVE_PUBLISH_WEBHOOK_SECRET_${tenant^^} from the droplet .env"
    fail=1
  fi
done

if [[ "$fail" -eq 0 ]]; then
  echo "PASS: all publish-webhook secrets are wired."
  exit 0
fi

echo
echo "An apply from here would ZERO the publish-webhook secret(s) above." >&2
echo "Both halves must stay byte-identical, so a zeroed secret silently kills" >&2
echo "the tenant's publish path (sender raises pre-write, receiver 401s)." >&2
echo >&2
echo "Fix: create the missing 1Password items in vault \`Grove QA\` -- one per" >&2
echo "tenant, item grove-publish-webhook-<tenant>-qa, field \`secret\` -- then" >&2
echo "uncomment the matching TF_VAR_ ref in $ENV_OP. Needs vault WRITE (the ops" >&2
echo "service account is read-only). Tracked in GOL-2518." >&2
echo >&2
echo "Override (you accept zeroing these secrets):" >&2
echo "  ALLOW_EMPTY_PUBLISH_SECRETS=1 make qa-l3-up" >&2

if [[ -n "${ALLOW_EMPTY_PUBLISH_SECRETS:-}" ]]; then
  echo
  echo "ALLOW_EMPTY_PUBLISH_SECRETS is set -- continuing anyway."
  exit 0
fi
if [[ -n "${PUBLISH_SECRET_GUARD_WARN_ONLY:-}" ]]; then
  echo
  echo "PUBLISH_SECRET_GUARD_WARN_ONLY is set -- reporting only, not failing."
  exit 0
fi
exit 1
