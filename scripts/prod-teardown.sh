#!/usr/bin/env bash
#
# prod-teardown.sh — MODULAR, backup-gated teardown for environments/production.
#
#   ./prod-teardown.sh list                    # show modules + what each touches
#   ./prod-teardown.sh plan   <module>         # read-only `terraform plan -destroy`
#   ./prod-teardown.sh destroy <module>        # gated destroy (typed confirm + backup proof)
#
# DESIGN — deliberately stricter than qa-l3-teardown.sh:
#
#   1. MODULAR. There is no "all". You tear down ONE module at a time, by name.
#      Taking down a single storefront must not be able to take the rest with it.
#
#   2. PROTECTED SET IS ABSOLUTE. The Managed PG cluster, both block volumes,
#      the reserved IPs, every Spaces bucket, and ALL Cloudflare/DNS resources
#      are unreachable from this script. They are not a mode, not a flag, not a
#      prompt. Prod DNS spans four LIVE brand zones; the QA lesson
#      (qa-teardown-dns.sh, never run because it kills the live zone) is encoded
#      here as "DNS is not a teardown target, full stop."
#
#   3. BACKUP-GATED. Stateful modules (odoo, blogs) refuse to run until a FRESH,
#      NON-TRIVIAL backup is proven to exist in the module's Spaces bucket.
#      The gate FAILS CLOSED: if the check cannot run (no creds, no s3cmd, no
#      1Password session), it aborts. A backup gate that silently passes is
#      worse than no gate.
#
#   4. TYPED CONFIRMATION per module, naming the module and the environment.
#
# KNOWN STATE 2026-08-07: `grove-odoo-backups` holds 26 objects / ~64 KiB, versus
# `grove-blogs-backups` at 256 objects / ~109 MiB. GOL-99 wired the nightly job
# but GOL-830 (droplet-replace survival test + backup monitoring) is still open
# and GOL-854 (dead-man's-switch) is blocked. The odoo gate below is EXPECTED TO
# FAIL today. That is the gate working, not a bug in this script.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/infra/terraform/environments/production"
ENV_FILE="$TF_DIR/.env.op"
SPACES_ENDPOINT="https://nyc3.digitaloceanspaces.com"

# Freshness + size floor a backup must clear before a stateful module may go.
BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-26}"   # nightly job + 2h grace
BACKUP_MIN_BYTES="${BACKUP_MIN_BYTES:-10485760}"     # 10 MiB — 64 KiB must not pass

say() { printf '%s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }
hr()  { printf '%s\n' "------------------------------------------------------------"; }

# ---------------------------------------------------------------------------
# Module registry.  targets = terraform -target addresses.  bucket = backup gate
# ("-" means stateless, no gate).  Anything NOT listed here cannot be destroyed.
# ---------------------------------------------------------------------------
module_targets() {
  case "$1" in
    app-hub)       echo '-target=digitalocean_app.hub' ;;
    app-goldberry) echo '-target=digitalocean_app.tenant["goldberry"]' ;;
    app-ggg)       echo '-target=digitalocean_app.tenant["ggg"]' ;;
    app-nursery)   echo '-target=digitalocean_app.tenant["nursery"]' ;;
    odoo)          echo '-target=digitalocean_droplet.odoo -target=digitalocean_volume_attachment.odoo_filestore -target=digitalocean_reserved_ip_assignment.odoo -target=digitalocean_firewall.odoo' ;;
    blogs)         echo '-target=digitalocean_droplet.blogs -target=digitalocean_volume_attachment.blogs_data -target=digitalocean_reserved_ip_assignment.blogs -target=digitalocean_firewall.blogs' ;;
    *)             return 1 ;;
  esac
}
module_bucket() {
  case "$1" in
    odoo)  echo "grove-odoo-backups" ;;
    blogs) echo "grove-blogs-backups" ;;
    *)     echo "-" ;;
  esac
}
module_desc() {
  case "$1" in
    app-hub)       echo "App Platform app grove-hub-prod (stateless; rebuild = re-apply)" ;;
    app-goldberry) echo "App Platform app grove-goldberry-prod (stateless)" ;;
    app-ggg)       echo "App Platform app grove-ggg-prod (stateless)" ;;
    app-nursery)   echo "App Platform app grove-nursery-prod (stateless)" ;;
    odoo)          echo "Prod Odoo droplet + its volume ATTACHMENT, reserved-IP assignment, firewall" ;;
    blogs)         echo "Prod blogs droplet + its volume ATTACHMENT, reserved-IP assignment, firewall" ;;
  esac
}

PROTECTED='digitalocean_database_cluster.pg   Managed Postgres — the revenue database
digitalocean_database_db.*          Odoo + postgres databases
digitalocean_volume.odoo_filestore  product photos / ir.attachment binaries
digitalocean_volume.blogs_data      Ghost content + MySQL data
digitalocean_reserved_ip.*          IPs the live DNS records point at
digitalocean_spaces_bucket.*        ALL backup buckets
cloudflare_*                        every DNS record, ruleset and origin cert (4 LIVE brand zones)
tls_* / origin CA                   origin certificates'

cmd_list() {
  say "Modules (destroy exactly one at a time):"; hr
  for m in app-hub app-goldberry app-ggg app-nursery odoo blogs; do
    b="$(module_bucket "$m")"
    printf '  %-14s %s\n' "$m" "$(module_desc "$m")"
    [ "$b" != "-" ] && printf '  %-14s   backup gate: %s (max age %sh, min %s bytes)\n' "" "$b" "$BACKUP_MAX_AGE_HOURS" "$BACKUP_MIN_BYTES"
  done
  hr
  say "NEVER destroyable by this script (no flag enables these):"
  printf '%s\n' "$PROTECTED" | sed 's/^/  /'
  hr
  say "Volumes survive by design: only the ATTACHMENT is destroyed, so the droplet"
  say "can be rebuilt and re-attached with data intact. Volumes also carry"
  say "prevent_destroy in terraform as a second line of defence."
}

# --- backup gate: fail closed on every uncertainty -------------------------
check_backup() {
  local bucket="$1"
  say "BACKUP GATE — $bucket"
  command -v s3cmd >/dev/null || die "s3cmd not installed; cannot prove a backup exists (gate fails closed)"
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; cannot load Spaces credentials (gate fails closed)"
  command -v op >/dev/null || die "1Password CLI not found (gate fails closed)"

  local key secret
  key="$(op run --env-file="$ENV_FILE" -- printenv SPACES_KEY 2>/dev/null || true)"
  secret="$(op run --env-file="$ENV_FILE" -- printenv SPACES_SECRET 2>/dev/null || true)"
  # op read fails OPEN (exit 0 + empty string) — emptiness must be fatal.
  [ -n "$key" ] && [ -n "$secret" ] || die "Spaces credentials came back EMPTY — run 'op signin' (gate fails closed)"

  local listing
  listing="$(s3cmd --access_key="$key" --secret_key="$secret" \
      --host="${SPACES_ENDPOINT#https://}" --host-bucket="%(bucket)s.${SPACES_ENDPOINT#https://}" \
      ls "s3://${bucket}/" --recursive 2>/dev/null || true)"
  [ -n "$listing" ] || die "bucket $bucket is EMPTY or unreadable — refusing to destroy a stateful module"

  # newest object: s3cmd prints "YYYY-MM-DD HH:MM  <bytes>  s3://..."
  local newest ts bytes age_h
  newest="$(printf '%s\n' "$listing" | sort -r | head -1)"
  ts="$(printf '%s' "$newest" | awk '{print $1" "$2}')"
  bytes="$(printf '%s' "$newest" | awk '{print $3}')"
  say "  newest object: $ts  ${bytes} bytes"

  age_h="$(( ( $(date +%s) - $(date -j -f '%Y-%m-%d %H:%M' "$ts" +%s 2>/dev/null || date -d "$ts" +%s) ) / 3600 ))"
  say "  age: ${age_h}h (limit ${BACKUP_MAX_AGE_HOURS}h)   size floor: ${BACKUP_MIN_BYTES} bytes"

  [ "$age_h" -le "$BACKUP_MAX_AGE_HOURS" ] \
    || die "newest backup is ${age_h}h old (>${BACKUP_MAX_AGE_HOURS}h). Backups are stale — fix the nightly job (GOL-830/GOL-854) before tearing anything down."
  [ "$bytes" -ge "$BACKUP_MIN_BYTES" ] \
    || die "newest backup is only ${bytes} bytes (<${BACKUP_MIN_BYTES}). That is not a real backup — see GOL-830. Refusing."
  say "  PASS"
}

tf_init() {
  cat > "$TF_DIR/backend.hcl" <<'EOF'
endpoint                    = "https://nyc3.digitaloceanspaces.com"
bucket                      = "grove-tf-state"
key                         = "production/terraform.tfstate"
region                      = "us-east-1"
skip_credentials_validation = true
skip_metadata_api_check     = true
skip_region_validation      = true
skip_requesting_account_id  = true
force_path_style            = true
EOF
  ( cd "$TF_DIR" && op run --env-file="$ENV_FILE" -- terraform init -reconfigure -backend-config=backend.hcl -input=false >/dev/null )
}

cmd_plan() {
  local m="$1"; local t; t="$(module_targets "$m")" || die "unknown module '$m' (see: $0 list)"
  say "=== PLAN -destroy — module '$m' (READ-ONLY) ==="
  say "  $(module_desc "$m")"; hr
  tf_init
  # shellcheck disable=SC2086
  ( cd "$TF_DIR" && op run --env-file="$ENV_FILE" -- terraform plan -destroy $t -input=false )
  hr
  say "Read the plan above. Nothing was changed."
  say "If it proposes destroying ANY protected resource, STOP and report it —"
  say "that means -target leaked past this script's module boundary."
}

cmd_destroy() {
  local m="$1"; local t; t="$(module_targets "$m")" || die "unknown module '$m' (see: $0 list)"
  local b; b="$(module_bucket "$m")"

  say "=== DESTROY — production / module '$m' ==="
  say "  $(module_desc "$m")"; hr
  say "Survives regardless:"; printf '%s\n' "$PROTECTED" | sed 's/^/  /'; hr

  [ "$b" != "-" ] && { check_backup "$b"; hr; }

  say "Step 1 of 2 — confirm the module."
  printf "  Type the module name '%s': " "$m"; read -r c1
  [ "$c1" = "$m" ] || die "module name mismatch"

  say "Step 2 of 2 — confirm the environment. This is PRODUCTION."
  printf "  Type 'destroy-production-%s': " "$m"; read -r c2
  [ "$c2" = "destroy-production-$m" ] || die "confirmation mismatch"

  hr; say "running terraform destroy (targeted) ..."
  tf_init
  # shellcheck disable=SC2086
  ( cd "$TF_DIR" && op run --env-file="$ENV_FILE" -- terraform destroy $t -input=false )
  hr
  say "Module '$m' destroyed. Rebuild: targeted 'terraform apply' of the same addresses."
  [ "$m" = "odoo" ] && say "NOTE: re-attach the filestore volume on rebuild, or Odoo comes up with no product photos."
  return 0
}

case "${1:-}" in
  list)    cmd_list ;;
  plan)    [ $# -eq 2 ] || die "usage: $0 plan <module>";    cmd_plan "$2" ;;
  destroy) [ $# -eq 2 ] || die "usage: $0 destroy <module>"; cmd_destroy "$2" ;;
  *) say "usage: $0 {list|plan <module>|destroy <module>}"; say ""; cmd_list; exit 2 ;;
esac
