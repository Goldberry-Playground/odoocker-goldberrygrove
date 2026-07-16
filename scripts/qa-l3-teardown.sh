#!/usr/bin/env bash
# Teardown for the Level 3 QA environment (infra/terraform/environments/
# qa-app-platform). The monolith's qa-teardown workflow died with the
# monolith -- this is its L3 successor, run LOCALLY (destroys are not CI
# material: the drift workflow's token deliberately can't delete).
#
# Two modes:
#   compute  - destroy the spend, keep the data + DNS:
#              4 App Platform apps + 2 droplets + BOTH volume attachments
#              (caddy_data and odoo_filestore -- the volumes themselves
#              survive; only the attachments drop, and `make qa-l3-up`
#              reattaches them). Managed PG (all Odoo data), the
#              caddy-data volume (LE certs -- rate-limit protection, see
#              ADR-005), the DNS zone, and the reserved IP all survive.
#              Re-create with `make qa-l3-up`; the droplets re-bootstrap
#              unattended from cloud-init and Odoo reconnects to the
#              surviving DB.
#   all      - terraform destroy of EVERYTHING, including Managed PG
#              (IRREVERSIBLE data loss), the LE cert volume (next
#              deploy re-issues against the LE rate limit budget), the
#              qa DNS zone, and the Cloudflare NS delegation records.
#              QA holds REAL order/inventory data (system of record
#              since 2026-07-09), so the PG cluster and filestore
#              volume carry `prevent_destroy` guards (#237): terraform
#              will refuse `all` mode until those guards are removed
#              in a reviewed PR. That refusal is the intended behavior,
#              not a bug.
#
# Usage:
#   bash scripts/qa-l3-teardown.sh compute
#   bash scripts/qa-l3-teardown.sh all
#
# Requirements:
#   - op CLI signed in, with read access to the `Goldberry Grove - Admin`
#     vault. Every secret this script needs is declared as an op:// ref in
#     $TF_DIR/.env.op; `op run` resolves them and injects them as TF_VAR_*
#     / AWS_* for the wrapped terraform. Infisical is retired (GOL-231);
#     this script was one of its last local-ops consumers (GOL-418).
#
# Known footguns (learned the hard way, 2026-06/07):
#   - The DO provider has an internal ~1m delete timeout that `timeouts`
#     blocks canNOT override. If a droplet delete trips it, terraform
#     errors but state stays consistent -- just re-run this script; the
#     second pass finds the droplet gone and continues.
#   - DO PATs can have silent scope gaps: a token that creates+reads
#     fine may 403 on destroy for firewalls/domains/volumes. If `all`
#     mode 403s, mint a full-scope token and re-run with
#     DIGITALOCEAN_TOKEN_OVERRIDE=<token> (never paste tokens into logs).
set -euo pipefail

MODE="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/infra/terraform/environments/qa-app-platform"
ENV_FILE="$TF_DIR/.env.op"

case "$MODE" in
  compute|all) ;;
  *) echo "usage: $0 {compute|all}"; exit 2 ;;
esac

if [ "$MODE" = "all" ]; then
  echo "!! 'all' destroys Managed PG (ALL Odoo data), the LE cert volume,"
  echo "!! the qa DNS zone, and the Cloudflare NS delegation."
else
  echo "'compute' destroys 15 resources: 4 App Platform apps, 2 droplets,"
  echo "2 volume attachments (caddy_data + odoo_filestore), plus their"
  echo "DEPENDENTS terraform pulls in via -target: droplet firewalls, the"
  echo "odoo/oo/keep/apex DNS records, and the PG trusted-sources firewall"
  echo "(re-verified via plan -destroy 2026-07-15, GOL-418)."
  echo "Survives: Managed PG cluster+data, the LE-cert + filestore volumes,"
  echo "the reserved IP, the qa DNS zone + CF delegation. NOTE: with the PG"
  echo "firewall destroyed the DB endpoint is password-only until rebuild."
  echo "Rebuild: make qa-l3-up"
fi
printf "Type 'destroy-qa-l3-%s' to continue: " "$MODE"
read -r CONFIRM
[ "$CONFIRM" = "destroy-qa-l3-$MODE" ] || { echo "aborted"; exit 1; }

# Regenerate the gitignored backend config so a clean checkout works and the
# config cannot drift from CI's. Credentials are not written here -- the S3
# backend reads the AWS_* vars `op run` injects below.
cat > "$TF_DIR/backend.hcl" <<'EOF'
endpoint                    = "https://nyc3.digitaloceanspaces.com"
bucket                      = "grove-tf-state"
key                         = "qa-app-platform/terraform.tfstate"
region                      = "us-east-1"
skip_credentials_validation = true
skip_metadata_api_check     = true
skip_region_validation      = true
skip_requesting_account_id  = true
force_path_style            = true
EOF

TARGETS=""
if [ "$MODE" = "compute" ]; then
  # -target on the bare for_each address (digitalocean_app.tenant)
  # covers all its instances.
  TARGETS="-target=digitalocean_app.hub -target=digitalocean_app.tenant -target=digitalocean_volume_attachment.caddy_data -target=digitalocean_droplet.odoo -target=digitalocean_droplet.obs"
fi

echo "==> terraform destroy ($MODE)..."
# `op run` resolves the op:// refs in .env.op and injects AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY (state backend) plus the TF_VAR_* the env needs.
# DIGITALOCEAN_TOKEN_OVERRIDE is exported from THIS shell (not .env.op), so it
# is visible to the wrapped bash and still wins when set.
op run --env-file="$ENV_FILE" -- bash -c '
  set -euo pipefail
  # Full-scope override for `all` mode when the standard token 403s on
  # firewall/domain/volume deletes (silent DO PAT scope gaps).
  export TF_VAR_do_token="${DIGITALOCEAN_TOKEN_OVERRIDE:-$TF_VAR_do_token}"
  # Optional passthrough -- no 1Password home yet (GOL-293), so it is not in
  # .env.op. The TF var defaults to "" when unset.
  export TF_VAR_grove_brand_pr_token="${GROVE_BRAND_PR_TOKEN:-}"
  terraform -chdir="'"$TF_DIR"'" init -backend-config=backend.hcl -input=false >/dev/null
  # -auto-approve is safe here: this script already required the typed
  # destroy-qa-l3-<mode> confirmation above.
  terraform -chdir="'"$TF_DIR"'" destroy '"$TARGETS"' -input=false -auto-approve
'

echo "==> Post-destroy state summary:"
op run --env-file="$ENV_FILE" -- bash -c '
  terraform -chdir="'"$TF_DIR"'" state list || true
'
echo "Done. Rebuild any time with: make qa-l3-up"
