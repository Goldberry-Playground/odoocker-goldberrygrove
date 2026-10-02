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
# Deliberately does NOT need Terraform, the S3 backend, or a plan: it reads the
# declared sources straight out of this env's HCL and compares them against the
# DO API with a read-only token. So it runs from anywhere (operator box, agent
# plane, CI) as a pre-apply check and as the post-apply proof, without touching
# state -- the env declares ~20 required vars, most of them secrets, so a real
# `terraform plan` here cannot be cronned honestly (GOL-2564).
#
# SOURCE PRECEDENCE mirrors Terraform's own (GOL-2631): an explicit assignment
# in terraform.tfvars WINS over the variables.tf default. Reading only the
# defaults would compare live DO against values that were never applied and
# report false drift on exactly the variable that drifted. The resolved
# provenance of every list is printed, so the operator can see at a glance
# whether a tfvars value is shadowing a codified default.
#
# It compares, per port, the UNION of allowed sources (that is the
# security-meaningful question: "who can reach 5080?"), not rule-by-rule --
# DO is free to split or merge rules that carry the same port.
#
# MIRRORS the `digitalocean_firewall "obs"` block in ../main.tf. If you add or
# repoint an inbound rule there, update EXPECTED_PORTS below in the same commit.
# If a variable this script reads is renamed or loses its default, the script
# fails LOUDLY as exit 2 ("bad env") rather than exiting 1 ("drift") -- a false
# DRIFT alert is never retried by .github/workflows/obs-firewall-drift.yml,
# so a broken check must not be able to impersonate one.
#
# Usage:
#   infra/terraform/environments/observability/scripts/check-firewall.sh
#
# Env:
#   DO_TOKEN | DIGITALOCEAN_TOKEN | TF_VAR_do_token   required; read-only is enough
#   TFVARS    override the tfvars path (default ../terraform.tfvars; absent is fine)
#   FW_NAME   override the firewall name (default grove-obs-fw)
#
# Exit codes:
#   0  live firewall matches the declared sources
#   1  drift detected (report says which port, missing vs. unexpected)
#   2  bad env: no token, unreadable/renamed vars, firewall absent, DO unreachable
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VARS="$HERE/variables.tf"
TFVARS="${TFVARS:-$HERE/terraform.tfvars}"
FW_NAME="${FW_NAME:-grove-obs-fw}"

[ -f "$VARS" ] || { echo "::error::not found: $VARS" >&2; exit 2; }

DO_TOKEN="${DO_TOKEN:-${DIGITALOCEAN_TOKEN:-${TF_VAR_do_token:-}}}"
if [ -z "$DO_TOKEN" ]; then
  echo "::error::No DO API token in env (checked DO_TOKEN, DIGITALOCEAN_TOKEN, TF_VAR_do_token)" >&2
  exit 2
fi

# ── HCL readers ───────────────────────────────────────────────────────────────
# Every one of these ends in `|| true` on the final grep: a no-match `grep -o`
# exits 1, and under `set -e` inside a command substitution that aborts the
# whole script with a bare exit 1 and NO output -- indistinguishable from
# "drift detected" to the caller, and it would skip the diagnostics below.

tf_has_variable() { grep -qE "^variable \"$1\" \\{" "$VARS"; }

# Is there a `default =` assignment inside variable "$1"? Anchored the same way
# as tf_default_list so a description mentioning the word stays harmless.
tf_has_default() {
  awk -v name="$1" '
    $0 ~ "^variable \"" name "\" \\{" { inblock = 1; next }
    inblock && /^}/                   { exit }
    inblock {
      line = $0
      sub(/#.*/, "", line)
      if (line ~ /^[ \t]*default[ \t]*=/) { found = 1; exit }
    }
    END { exit(found ? 0 : 1) }
  ' "$VARS"
}

# Pull one variable's `default = [...]` list out of variables.tf as a sorted
# set, one entry per line. Anchored on `default` at the start of the line so a
# description that merely says the word "default" can't be mistaken for the
# assignment, and comments are stripped so a commented-out CIDR never counts as
# declared.
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
  ' "$VARS" | { grep -o '"[^"]*"' || true; } | tr -d '"' | sort -u
}

# Is `<name> =` assigned at top level in the tfvars file? A missing file is
# simply "not assigned" -- tfvars is gitignored and absent in CI.
hcl_has_assign() {
  [ -f "$TFVARS" ] || return 1
  grep -qE "^[ \t]*$1[ \t]*=" "$TFVARS"
}

# Values of a (possibly multi-line) `<name> = [...]` assignment in the tfvars.
hcl_assign_list() {
  awk -v name="$1" '
    !ina && $0 ~ "^[ \t]*" name "[ \t]*=" { ina = 1 }
    ina {
      line = $0
      sub(/#.*/, "", line)
      sub(/\/\/.*/, "", line)
      print line
      if (line ~ /\]/) exit
    }
  ' "$TFVARS" | { grep -o '"[^"]*"' || true; } | tr -d '"' | sort -u
}

# Resolve one source list the way Terraform would. Sets VAL + SRCLABEL rather
# than echoing, so a `return 2` is not swallowed by a command substitution.
# An explicitly empty list in tfvars (`ingest_source_cidrs = []`) is a REAL
# value meaning "admin-only", distinct from "not assigned" -- hence the
# separate presence test.
VAL=""
SRCLABEL=""
resolve_list() {
  local name="$1"
  if hcl_has_assign "$name"; then
    SRCLABEL="$(basename "$TFVARS")"
    VAL="$(hcl_assign_list "$name")"
    return 0
  fi
  if ! tf_has_variable "$name"; then
    echo "::error::$VARS declares no variable \"$name\" -- this script mirrors ../main.tf and must be updated in the same commit as a rename" >&2
    return 2
  fi
  if ! tf_has_default "$name"; then
    echo "::error::variable \"$name\" has no default in $VARS and is not set in $TFVARS -- cannot know what was applied, so drift is unknowable (supply it via TFVARS)" >&2
    return 2
  fi
  SRCLABEL="variables.tf default"
  VAL="$(tf_default_list "$name")"
  return 0
}

resolve_list admin_ip_cidrs           || exit 2; admin="$VAL";       src_admin="$SRCLABEL"
resolve_list automation_ssh_cidrs     || exit 2; automation="$VAL";  src_automation="$SRCLABEL"
resolve_list ingest_source_cidrs      || exit 2; ingest="$VAL";      src_ingest="$SRCLABEL"
resolve_list ingest_source_tags       || exit 2; ingest_tags="$VAL"; src_tags="$SRCLABEL"
resolve_list cloudflare_ingress_cidrs || exit 2; cf="$VAL";          src_cf="$SRCLABEL"

# Only admin is load-bearing enough that empty is nonsense: the other three all
# document "Empty = admin-only" as a legitimate posture.
[ -n "$admin" ] || {
  echo "::error::admin_ip_cidrs resolved to EMPTY (source: $src_admin) -- refusing to grade a firewall with no admin access as clean" >&2
  exit 2
}

echo "declared sources: admin<-$src_admin  automation<-$src_automation  ingest<-$src_ingest  tags<-$src_tags  cf<-$src_cf"
case "$src_admin" in
  variables.tf*) ;;
  *) echo "  ! admin_ip_cidrs comes from $src_admin, shadowing the variables.tf default -- that shadowing is how grove-obs-fw drifted (ADR-010); prefer codifying it" ;;
esac

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
  echo "OK: live $FW_NAME matches the declared sources"
  exit 0
fi
cat >&2 <<'EOF'

Drift detected. Reconcile from code (never by hand in the DO UI):
  terraform -chdir=infra/terraform/environments/observability plan  -target=digitalocean_firewall.obs
  terraform -chdir=infra/terraform/environments/observability apply -target=digitalocean_firewall.obs
Check the "declared sources:" line above first. If admin/ingest/automation came
from terraform.tfvars rather than the variables.tf default, the tfvars value is
what will be applied -- a tfvars value silently beats the codified default, and
that shadowing is how grove-obs-fw drifted to the lone stale 74.47.41.38/32
while prod/QA carried Josh's rotated address (ADR-010).
EOF
exit 1
