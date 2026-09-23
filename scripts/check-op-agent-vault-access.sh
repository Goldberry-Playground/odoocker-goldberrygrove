#!/usr/bin/env bash
# check-op-agent-vault-access.sh -- Verify the ops 1Password service account can
# actually CREATE, EDIT and DELETE items in the agent-managed QA vault.
#
# WHY THIS EXISTS (GOL-2526, 2026-09-23):
# The ops service account (integration WMSDNFU3FRCXVKGDQ7FTFKCCRI) is read-only
# on all three vaults it can see AND cannot create a vault of its own
# (`op vault create` -> 403 at the account level). So any work that needs a
# secret to EXIST stops until a human writes it -- GOL-2518, GOL-2424,
# GOL-1643, GOL-697 and GOL-2318 all queued behind the same five minutes.
# GOL-2526 asks Josh for a dedicated agent-writable vault. This script is the
# verification half: run it right after the grant to prove the access landed,
# instead of discovering mid-provision that it didn't.
#
# Read-only probing cannot answer the question -- 1Password reports nothing
# about a service account's own permissions -- so the check is a real
# create/edit/delete round trip on a throwaway item. It therefore MUST NOT be
# pointed at a human-managed vault, and refuses to be (see GUARD below).
#
# Usage:
#   scripts/check-op-agent-vault-access.sh
#   OP_AGENT_VAULT="Some Other Agent Vault" scripts/check-op-agent-vault-access.sh
#
# Exit codes:
#   0  write access confirmed (create + edit + delete all succeeded)
#   1  vault is visible but NOT writable -- the grant is missing or incomplete
#   2  vault not visible to this service account -- vault missing, or not shared
#   3  refused -- guard tripped, `op` missing, or not signed in
#
# Prints no secret values. The probe item's only field is a fixed literal.

set -euo pipefail

VAULT="${OP_AGENT_VAULT:-Grove QA - Agent Managed}"

# --- GUARD ------------------------------------------------------------------
# Never run a write probe against a vault humans manage. This is a hard stop:
# there is no override flag, because the only reason to point this script at
# `Grove QA` would be to test option B, and a create/delete round trip is not
# worth the risk of colliding with a live secret like stripe-nursery-qa.
PROTECTED_VAULTS=(
  "Grove QA"
  "Grove Prod"
  "Grove Production"
  "Goldberry Grove - Admin"
  "Private"
  "Personal"
)
for protected in "${PROTECTED_VAULTS[@]}"; do
  if [[ "${VAULT}" == "${protected}" ]]; then
    echo "REFUSED: '${VAULT}' is a human-managed vault; this script only probes agent-managed vaults." >&2
    echo "         Set OP_AGENT_VAULT to the agent vault (default: 'Grove QA - Agent Managed')." >&2
    exit 3
  fi
done

command -v op >/dev/null 2>&1 || { echo "REFUSED: 1Password CLI 'op' not on PATH." >&2; exit 3; }
op whoami >/dev/null 2>&1 || { echo "REFUSED: 'op whoami' failed -- no OP_SERVICE_ACCOUNT_TOKEN / not signed in." >&2; exit 3; }

if ! op vault get "${VAULT}" >/dev/null 2>&1; then
  echo "FAIL(2): vault '${VAULT}' is not visible to this service account."
  echo "         Either it was never created, or the service account was not granted access."
  echo "         See docs/RUNBOOK-1password-agent-vault.md for the two UI steps."
  exit 2
fi

ITEM="zz-agent-write-probe-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cleanup() { op item delete "${ITEM}" --vault "${VAULT}" >/dev/null 2>&1 || true; }

echo "probing vault '${VAULT}' with throwaway item '${ITEM}'..."

if ! op item create --category "Secure Note" --title "${ITEM}" --vault "${VAULT}" \
     "notesPlain=GOL-2526 write probe; safe to delete" >/dev/null 2>&1; then
  echo "FAIL(1): cannot CREATE items in '${VAULT}' -- the service account grant is missing 'create items'."
  exit 1
fi
trap cleanup EXIT

if ! op item get "${ITEM}" --vault "${VAULT}" >/dev/null 2>&1; then
  echo "FAIL(1): created the item but cannot READ it back in '${VAULT}'."
  exit 1
fi

if ! op item edit "${ITEM}" --vault "${VAULT}" \
     "notesPlain=GOL-2526 write probe (edited); safe to delete" >/dev/null 2>&1; then
  echo "FAIL(1): can create but cannot EDIT items in '${VAULT}' -- grant is missing 'edit items'."
  echo "         Rotation would be impossible; fix before storing anything here."
  exit 1
fi

if ! op item delete "${ITEM}" --vault "${VAULT}" >/dev/null 2>&1; then
  echo "FAIL(1): can create and edit but cannot DELETE in '${VAULT}'."
  echo "         Probe item '${ITEM}' is still there -- remove it by hand."
  exit 1
fi
trap - EXIT

echo "PASS: create + read + edit + delete all succeeded in '${VAULT}'."
echo "      Agents can now provision QA secrets without a human write."
echo "      Scope rules and naming: docs/RUNBOOK-1password-agent-vault.md"
