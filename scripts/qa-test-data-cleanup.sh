#!/usr/bin/env bash
# qa-test-data-cleanup — documented entrypoint.
#
# Tears QA test data (synthetic canary orders, checkout/cart journey orders,
# reserved-test-domain partners, the SYNTHETIC-CANARY product) back down to a
# clean, reproducible pre-freeze state. SURGICAL and system-of-record-safe:
# see scripts/qa-test-data-cleanup.py for the "only ever delete provably-fake
# data" rationale. DRY-RUN BY DEFAULT — pass --apply to actually delete.
#
# CREDENTIALS — two supported sources, auto-detected:
#
#   (a) Already-injected env (the on-droplet / CI path). If ODOO_DB, ODOO_LOGIN
#       and (ODOO_API_KEY or SYNTHETIC_ODOO_API_KEY) are already set — e.g. you
#       are on the QA obs droplet where the `synthetic` container's env exists,
#       or a workflow injected them — this runs directly with no `op`:
#           ODOO_XMLRPC_URL=http://odoo:8069 ODOO_DB=... ODOO_LOGIN=... \
#           SYNTHETIC_ODOO_API_KEY=... bash scripts/qa-test-data-cleanup.sh
#
#   (b) 1Password `op run` (local operator with Grove QA vault access). Point
#       QA_CLEANUP_ENV_OP at an op env-file whose op:// refs resolve ODOO_DB /
#       ODOO_LOGIN / ODOO_API_KEY (+ optional ODOO_XMLRPC_URL). A ready template
#       lives at scripts/qa-test-data-cleanup.env.op — fill in the item id, then:
#           QA_CLEANUP_ENV_OP=scripts/qa-test-data-cleanup.env.op \
#           bash scripts/qa-test-data-cleanup.sh --apply
#
# All flags after the script name pass straight through to the python worker:
#   (default)                  dry-run report — deletes nothing
#   --apply                    actually delete
#   --include-canary-product   also remove the SYNTHETIC-CANARY monitoring product
#   --allow-nonqa-db           bypass the QA-DB-name guard (dangerous)
#   --json                     machine-readable summary on stdout
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER="$HERE/qa-test-data-cleanup.py"
ENV_OP="${QA_CLEANUP_ENV_OP:-}"

have_direct_creds() {
  [ -n "${ODOO_DB:-}" ] && [ -n "${ODOO_LOGIN:-}" ] && \
    { [ -n "${ODOO_API_KEY:-}" ] || [ -n "${SYNTHETIC_ODOO_API_KEY:-}" ]; }
}

if [ -n "$ENV_OP" ]; then
  command -v op >/dev/null 2>&1 || { echo "ERROR: op CLI not found but QA_CLEANUP_ENV_OP is set" >&2; exit 1; }
  [ -f "$ENV_OP" ] || { echo "ERROR: QA_CLEANUP_ENV_OP file not found: $ENV_OP" >&2; exit 1; }
  echo "==> resolving QA Odoo creds from 1Password ($ENV_OP)" >&2
  exec op run --env-file="$ENV_OP" -- python3 "$WORKER" "$@"
elif have_direct_creds; then
  echo "==> using QA Odoo creds already present in the environment" >&2
  exec python3 "$WORKER" "$@"
else
  cat >&2 <<'EOF'
ERROR: no QA Odoo credentials available.
  Provide EITHER:
    (a) ODOO_DB + ODOO_LOGIN + (ODOO_API_KEY | SYNTHETIC_ODOO_API_KEY) in the env, or
    (b) QA_CLEANUP_ENV_OP=<op env-file> and be `op` signed-in with Grove QA vault access.
  See the header of this script for full usage.
EOF
  exit 1
fi
