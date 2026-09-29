#!/usr/bin/env bash
# Assert the LIVE grove-obs-fw matches what this env's Terraform declares.
#
# Why this exists (GOL-2333 / GOL-1844): grove-obs is the canonical
# observability droplet (ADR-010) and it is the one long-lived env with no
# drift watcher -- .github/workflows/terraform-drift.yml only plans
# qa-app-platform. grove-obs-fw drifted unnoticed for weeks: the admin
# allowlist kept only the stale 74.47.41.38/32 (Josh's rotated
# 173.84.140.152/32 missing), and 5080 still admitted 167.71.109.184/32 --
# grove-qa-l3-odoo's egress IP BEFORE its 2026-09-08 rebuild, which both
# admitted a non-Grove address and left the QA collector BLOCKED from ingest.
#
# Deliberately does NOT need Terraform, the S3 backend, or the env's
# terraform.tfvars: it reads the declared defaults straight out of
# variables.tf and compares them against the DO API with a read-only token.
# So it runs from anywhere (operator box, agent plane, CI) as a pre-apply
# check and as the post-apply proof, without touching state.
#
# It compares, per port, the UNION of allowed sources (that is the
# security-meaningful question: "who can reach 5080?"), not rule-by-rule --
# DO is free to split or merge rules that carry the same port.
#
# MIRRORS the `digitalocean_firewall "obs"` block in ../main.tf. If you add or
# repoint an inbound rule there, update EXPECTED_PORTS below in the same commit.
#
# Usage:
#   infra/terraform/environments/observability/scripts/check-firewall.sh
#
# Env required:
#   DO_TOKEN | DIGITALOCEAN_TOKEN | TF_VAR_do_token   read-only is enough
#
# Exit codes:
#   0  live firewall matches variables.tf
#   1  drift detected (report says which port, missing vs. unexpected)
#   2  bad env: no token, no variables.tf, firewall absent, or DO unreachable
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VARS="$HERE/variables.tf"
FW_NAME="${FW_NAME:-grove-obs-fw}"

[ -f "$VARS" ] || { echo "::error::not found: $VARS" >&2; exit 2; }

DO_TOKEN="${DO_TOKEN:-${DIGITALOCEAN_TOKEN:-${TF_VAR_do_token:-}}}"
if [ -z "$DO_TOKEN" ]; then
  echo "::error::No DO API token in env (checked DO_TOKEN, DIGITALOCEAN_TOKEN, TF_VAR_do_token)" >&2
  exit 2
fi

# Pull one variable's `default = [...]` list out of variables.tf as a sorted
# set, one entry per line. Anchored on `^  default` so a description that
# merely says the word "default" can't be mistaken for the assignment, and
# comments are stripped so a commented-out CIDR never counts as declared.
tf_default_list() {
  awk -v name="$1" '
    $0 ~ "^variable \"" name "\" \\{" { inblock = 1; next }
    inblock && /^}/                   { exit }
    inblock {
      line = $0
      sub(/#.*/, "", line)
      if (line ~ /^[ \t]*default[ \t]*=/) indef = 1
      if (indef) print line
      if (indef && line ~ /\]/) exit
    }
  ' "$VARS" | grep -o '"[^"]*"' | tr -d '"' | sort -u
}

admin="$(tf_default_list admin_ip_cidrs)"
automation="$(tf_default_list automation_ssh_cidrs)"
ingest="$(tf_default_list ingest_source_cidrs)"
ingest_tags="$(tf_default_list ingest_source_tags)"
cf="$(tf_default_list cloudflare_ingress_cidrs)"

[ -n "$admin" ] || { echo "::error::could not parse admin_ip_cidrs default out of $VARS" >&2; exit 2; }

# port -> declared source union. Mirrors ../main.tf.
expected_addrs() {
  case "$1" in
    22)   printf '%s\n%s\n' "$admin" "$automation" ;;
    5080) printf '%s\n%s\n' "$admin" "$ingest" ;;
    3034|8080) printf '%s\n' "$admin" ;;
    443)  printf '%s\n' "$cf" ;;
  esac | sed '/^$/d' | sort -u
}
expected_tags() {
  case "$1" in
    5080) printf '%s\n' "$ingest_tags" ;;
    *)    : ;;
  esac | sed '/^$/d' | sort -u
}
EXPECTED_PORTS="22 443 3034 5080 8080"

api() {
  curl -fsS --max-time 30 \
    -H "Authorization: Bearer $DO_TOKEN" \
    "https://api.digitalocean.com/v2/$1"
}

fws="$(api 'firewalls?per_page=200')" || { echo "::error::DO API unreachable" >&2; exit 2; }
fw="$(jq -c --arg n "$FW_NAME" '.firewalls[] | select(.name == $n)' <<<"$fws")"
if [ -z "$fw" ]; then
  echo "::error::no DO firewall named $FW_NAME (nothing applied?)" >&2
  exit 2
fi

status="$(jq -r '.status' <<<"$fw")"
droplets="$(jq -r '.droplet_ids | join(",")' <<<"$fw")"
echo "firewall: $FW_NAME  status=$status  droplet_ids=[$droplets]"
[ "$status" = "succeeded" ] || echo "  ! status is '$status', not 'succeeded' -- a prior apply may be incomplete"

drift=0
report() { # port, kind, marker, values...
  local port="$1" kind="$2" marker="$3"; shift 3
  for v in "$@"; do echo "  DRIFT :$port $marker $kind $v"; drift=1; done
}

live_ports="$(jq -r '.inbound_rules[].ports' <<<"$fw" | sort -u)"

for port in $EXPECTED_PORTS; do
  live_a="$(jq -r --arg p "$port" \
    '[.inbound_rules[] | select(.ports == $p) | .sources.addresses // [] | .[]] | unique | .[]' <<<"$fw" | sort -u)"
  live_t="$(jq -r --arg p "$port" \
    '[.inbound_rules[] | select(.ports == $p) | .sources.tags // [] | .[]] | unique | .[]' <<<"$fw" | sort -u)"
  exp_a="$(expected_addrs "$port")"
  exp_t="$(expected_tags "$port")"

  # shellcheck disable=SC2046  # word-splitting is the point: one arg per value
  report "$port" address MISSING     $(comm -23 <(echo "$exp_a") <(echo "$live_a"))
  # shellcheck disable=SC2046
  report "$port" address UNEXPECTED  $(comm -13 <(echo "$exp_a") <(echo "$live_a"))
  # shellcheck disable=SC2046
  report "$port" tag     MISSING     $(comm -23 <(echo "$exp_t") <(echo "$live_t"))
  # shellcheck disable=SC2046
  report "$port" tag     UNEXPECTED  $(comm -13 <(echo "$exp_t") <(echo "$live_t"))
done

# A port open live that main.tf never declares is the worst case -- it is an
# allow rule with no code behind it, so no review ever saw it.
for port in $live_ports; do
  case " $EXPECTED_PORTS " in
    *" $port "*) ;;
    *) echo "  DRIFT :$port UNDECLARED port open live (not in main.tf)"; drift=1 ;;
  esac
done

# A source tag that currently matches zero droplets is NOT drift -- tag rules
# exist so an immutably rebuilt droplet is re-admitted automatically. Surfaced
# because a permanently-empty tag means ingest is silently going nowhere.
for t in $ingest_tags; do
  n="$(api "tags/$t" 2>/dev/null | jq -r '.tag.resources.droplets.count // "absent"')"
  echo "note: ingest tag '$t' currently matches $n droplet(s)"
  [ "$n" = "absent" ] && { echo "  DRIFT tag '$t' does not exist in the DO account -- apply will fail"; drift=1; }
done

if [ "$drift" -eq 0 ]; then
  echo "OK: live $FW_NAME matches variables.tf"
  exit 0
fi
cat >&2 <<'EOF'

Drift detected. Reconcile from code (never by hand in the DO UI):
  terraform -chdir=infra/terraform/environments/observability plan  -target=digitalocean_firewall.obs
  terraform -chdir=infra/terraform/environments/observability apply -target=digitalocean_firewall.obs
First make sure the env's terraform.tfvars does NOT set admin_ip_cidrs /
ingest_source_cidrs / automation_ssh_cidrs -- a tfvars value silently beats the
variables.tf default, which is how this firewall drifted (ADR-010).
EOF
exit 1
