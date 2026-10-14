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
#              There is no obs droplet in this env any more:
#              grove-qa-l3-obs was retired 2026-09-29 (ADR-010 accepted,
#              GOL-2333). The canonical obs plane (grove-obs) lives in
#              environments/observability/ with its own state, out of
#              this script's reach.
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
  echo "DEPENDENTS terraform pulls in via -target: the Odoo droplet firewall"
  echo "and the odoo/apex DNS records."
  echo "Survives: Managed PG cluster+data, the LE-cert + filestore volumes,"
  echo "the reserved IP, the qa DNS zone + CF delegation, and -- since"
  echo "GOL-2581 -- the PG trusted-sources allowlist: its droplet leg is a"
  echo "TAG rule (digitalocean_tag.pg_client), so the allowlist is no longer"
  echo "a dependent of the droplet and the surviving cluster keeps its"
  echo "operator-CIDR network gate through the whole inter-train window."
  echo "Verify after the destroy (expect the ip_addr rules, not []):"
  echo "  doctl databases firewalls list \$(doctl databases list \\"
  echo "    --format Name,ID --no-header | awk '/grove-qa-l3-pg/{print \$2}')"
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
  # -reconfigure: backend.hcl is regenerated above on every run, so a stale
  # .terraform/ cache from an older generator (e.g. pre-force_path_style) must
  # not abort with "Backend configuration changed". Same bucket/key, so no
  # state migration is ever wanted here.
  # GOL-2584: `use_lockfile = true` is a NO-OP on DO Spaces (Spaces ignores
  # If-None-Match, so every concurrent run "acquires" the same lock). That is
  # how two QA teardowns both ran to completion on 2026-09-29, the second
  # dying only on "Error releasing the state lock ... 404". Terraform will not
  # stop a second destroy, so refuse here. Override: TF_LOCK_GUARD_OFF=1.
  bash "'"$REPO_ROOT"'/scripts/tf-state-lock-check.sh" guard qa-app-platform/terraform.tfstate
  terraform -chdir="'"$TF_DIR"'" init -reconfigure -backend-config=backend.hcl -input=false >/dev/null
  # -auto-approve is safe here: this script already required the typed
  # destroy-qa-l3-<mode> confirmation above.
  terraform -chdir="'"$TF_DIR"'" destroy '"$TARGETS"' -input=false -auto-approve
'

echo "==> Post-destroy state summary:"
op run --env-file="$ENV_FILE" -- bash -c '
  terraform -chdir="'"$TF_DIR"'" state list
'

# ── Post-destroy readback tripwire: PG trusted sources (GOL-2581) ────────────
# The surviving Managed PG cluster holds REAL order/inventory data (system of
# record since 2026-07-09) and stays up between release-train windows, so its
# trusted-sources allowlist is the ONLY network gate on its public endpoint
# once the droplet is gone. The allowlist used to be a terraform DEPENDENT of
# the droplet, so `destroy -target=digitalocean_droplet.odoo` silently took it
# with the droplet and the cluster sat at `trusted_sources = []` -- credential-
# only, for the whole inter-train window (found live 2026-09-29). The TF fix is
# a tag rule instead of a droplet-id rule (digitalocean_tag.pg_client), which
# removes the dependency edge; this is the readback that PROVES it held, in the
# GOL-2474 post-destroy-verification style. `state list` above cannot show it:
# a resource can be present in state and still have had its remote rules
# emptied, and absence in state does not prove the remote is open either -- only
# the live API answers that. Non-fatal by design (the destroy already
# succeeded); it prints a LOUD remediation line instead of failing a teardown
# that cannot be un-run.
echo "==> Trusted-sources readback on grove-qa-l3-pg:"
if ! command -v doctl >/dev/null 2>&1; then
  echo "!! doctl not on PATH -- SKIPPED the trusted-sources readback."
  echo "!! Check by hand: DO console > Databases > grove-qa-l3-pg > Settings"
  echo "!! > Trusted sources. It MUST NOT be empty."
else
  PG_ID="$(doctl databases list --format Name,ID --no-header 2>/dev/null \
             | awk '/^grove-qa-l3-pg[[:space:]]/{print $2}')"
  if [ -z "${PG_ID:-}" ]; then
    # Expected in `all` mode (the cluster is gone); a real problem in compute.
    if [ "$MODE" = "compute" ]; then
      echo "!! grove-qa-l3-pg NOT FOUND after a 'compute' teardown -- the"
      echo "!! cluster was supposed to survive. Investigate before rebuilding."
    else
      echo "grove-qa-l3-pg is gone (expected for mode 'all')."
    fi
  else
    RULES="$(doctl databases firewalls list "$PG_ID" --format Type,Value --no-header 2>/dev/null || true)"
    if [ -z "${RULES//[[:space:]]/}" ]; then
      echo "!! ============================================================"
      echo "!! EXPOSURE: grove-qa-l3-pg has NO trusted sources. Its public"
      echo "!! endpoint is now password-only, and it holds real order data."
      echo "!! This is the GOL-2581 regression -- the allowlist should have"
      echo "!! survived this teardown. Re-gate it NOW with the operator CIDRs"
      echo "!! from var.admin_ip_cidrs in the qa-app-platform env, e.g.:"
      echo "!!   doctl databases firewalls append $PG_ID \\"
      echo "!!     --rule ip_addr:<operator-ip>"
      echo "!! then fix the dependency edge before the next teardown."
      echo "!! ============================================================"
    else
      echo "OK -- trusted sources survived the teardown:"
      echo "$RULES" | sed 's/^/     /'
    fi
  fi
fi

echo "Done. Rebuild any time with: make qa-l3-up"
