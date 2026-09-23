#!/usr/bin/env bash
# Teardown for the Level 3 QA environment (infra/terraform/environments/
# qa-app-platform). The monolith's qa-teardown workflow died with the
# monolith -- this is its L3 successor, run LOCALLY (destroys are not CI
# material: the drift workflow's token deliberately can't delete).
#
# Two modes:
#   compute  - destroy the spend, keep the data + DNS:
#              4 App Platform apps + the Odoo droplet + BOTH volume
#              attachments (caddy_data and odoo_filestore -- the volumes
#              themselves survive; only the attachments drop, and
#              `make qa-l3-up` reattaches them). Managed PG (all Odoo
#              data), the caddy-data volume (LE certs -- rate-limit
#              protection, see ADR-005), the DNS zone, and the reserved
#              IP all survive.
#              The grove-qa-l3-obs droplet is EXEMPT by default
#              (GOL-2333 / GOL-2472, docs/ADR/010) -- it and its
#              firewall + oo/keep DNS records survive. Opt it back in
#              with QA_L3_TEARDOWN_OBS=1.
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
  echo "'compute' destroys: 4 App Platform apps, the Odoo droplet,"
  echo "2 volume attachments (caddy_data + odoo_filestore), plus their"
  echo "DEPENDENTS terraform pulls in via -target: the Odoo droplet firewall,"
  echo "the odoo/apex DNS records, and the PG trusted-sources firewall."
  if [ "${QA_L3_TEARDOWN_OBS:-0}" = "1" ]; then
    echo "QA_L3_TEARDOWN_OBS=1: ALSO the grove-qa-l3-obs droplet + its firewall"
    echo "and oo/keep DNS records (15 resources total, GOL-418 inventory)."
  else
    echo "grove-qa-l3-obs is EXEMPT (GOL-2333) and survives; set"
    echo "QA_L3_TEARDOWN_OBS=1 to include it. Check the plan count below."
  fi
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
  # App Platform park/scale leg (GOL-2327): Option A = DESTROY the 4 apps each
  # train. App Platform has no scale-to-zero for services, so parking would
  # still bill 4 x ~$5/mo min-tier while "down"; destroying zeroes that. Re-up
  # rebuilds from the pinned GHCR image (~2 min/app to ACTIVE+HTTP 200, apply in
  # parallel). The qa DNS zone, the per-app CNAME's parent zone, the reserved
  # IP, PG and both volumes SURVIVE -- see the header inventory and the
  # env README ("Release-train teardown: App Platform apps") for rationale.
  # -target on the bare for_each address (digitalocean_app.tenant)
  # covers all its instances.
  TARGETS="-target=digitalocean_app.hub -target=digitalocean_app.tenant -target=digitalocean_volume_attachment.caddy_data -target=digitalocean_droplet.odoo"
  # grove-qa-l3-obs is EXEMPT from the release-train teardown (GOL-2323 EPIC /
  # GOL-2333) until the CEO ratifies its fate in docs/ADR/010. Opt in with
  # QA_L3_TEARDOWN_OBS=1. NB: this is only the QA obs box -- the canonical obs
  # plane (grove-obs, environments/observability/) has its own state and is
  # never touched by this script.
  if [ "${QA_L3_TEARDOWN_OBS:-0}" = "1" ]; then
    TARGETS="$TARGETS -target=digitalocean_droplet.obs"
  fi

  # Fail-closed tripwire (GOL-2472). The exemption above is one `if` away from
  # being lost to a bad merge/rebase -- this asserts the built target list
  # actually honours it rather than trusting that the edit above survived.
  # It aborts BEFORE the destroy, so a regression costs a re-run, not a droplet.
  case "$TARGETS" in
    *digitalocean_droplet.obs*)
      if [ "${QA_L3_TEARDOWN_OBS:-0}" != "1" ]; then
        echo "FATAL: obs droplet is in the destroy targets but QA_L3_TEARDOWN_OBS is not 1." >&2
        echo "       The GOL-2333 teardown exemption has regressed -- refusing to destroy." >&2
        exit 3
      fi
      ;;
  esac
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
# Captured (not just printed) so the exemption check below can read it back.
# `set -e` would abort on a failed command substitution, so the rc is taken
# explicitly: "could not read state" must NOT be reported as "obs was destroyed".
STATE_LIST=""
STATE_RC=0
STATE_LIST="$(op run --env-file="$ENV_FILE" -- bash -c '
  terraform -chdir="'"$TF_DIR"'" state list
')" || STATE_RC=$?
printf '%s\n' "$STATE_LIST"

# Acceptance check for the exemption (GOL-2472): `-target` also destroys
# DEPENDENTS, so proving obs is absent from the target list is not the same as
# proving it survived. Read it back out of state.
if [ "$MODE" = "compute" ] && [ "${QA_L3_TEARDOWN_OBS:-0}" != "1" ]; then
  if [ "$STATE_RC" -ne 0 ]; then
    echo "WARN: could not read terraform state (rc=$STATE_RC) -- the obs exemption" >&2
    echo "      is UNVERIFIED. Re-run \`terraform state list\` before signing off." >&2
    exit 5
  fi
  MISSING=""
  for ADDR in digitalocean_droplet.obs digitalocean_firewall.obs \
               digitalocean_record.oo digitalocean_record.keep; do
    printf '%s\n' "$STATE_LIST" | grep -qx -- "$ADDR" || MISSING="$MISSING $ADDR"
  done
  if [ -n "$MISSING" ]; then
    echo "FATAL: exempt obs resource(s) GONE from state after teardown:$MISSING" >&2
    echo "       Expected them to survive (GOL-2333 / docs/ADR/010). Rebuild with" >&2
    echo "       \`make qa-l3-up\` and report on GOL-2472 before the next train." >&2
    exit 4
  fi
  echo "==> Exemption OK: obs droplet + firewall + oo/keep DNS records still in state."
fi

echo "Done. Rebuild any time with: make qa-l3-up"
