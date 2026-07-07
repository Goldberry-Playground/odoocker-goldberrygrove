#!/usr/bin/env bash
# Bootstrap the production blogs' Ghost instances - zero-touch.
#
# For each tenant, against the PUBLIC blog.* admin API:
#   1. If the instance is unclaimed, run owner setup (generated password)
#   2. Open a session (cookie auth - no JWT ceremony)
#   3. Ensure a "grove-ops" custom integration exists; read its
#      Admin API key + Content API key
#   4. Push password + keys to 1Password (Grove Infra item)
#
# Idempotent: already-claimed tenants are skipped IF their password is in
# 1Password (we log in to prove it); already-existing integrations are reused.
#
# Usage: scripts/blogs-bootstrap.sh [tenant ...]   (default: all four)
# Requires: curl, jq, op (signed in).
set -euo pipefail

VAULT="Goldberry Grove - Admin"
ITEM="Grove Infra"
OWNER_NAME="Josh Dunbar"
OWNER_EMAIL="josh@goldberrygrove.farm"
INTEGRATION_NAME="grove-ops"

tenants=("$@")
[ ${#tenants[@]} -eq 0 ] && tenants=(hub goldberry ggg nursery)

url_for() {
  case "$1" in
    hub) echo "https://blog.gatheringatthegrove.com" ;;
    goldberry) echo "https://blog.goldberrygrove.farm" ;;
    ggg) echo "https://blog.woodworkingeorge.com" ;;
    nursery) echo "https://blog.atthegrovenursery.com" ;;
    *) echo "unknown tenant: $1" >&2; exit 2 ;;
  esac
}

title_for() {
  case "$1" in
    hub) echo "Gathering at the Grove" ;;
    goldberry) echo "Goldberry Grove Farm" ;;
    ggg) echo "GGG Woodworking" ;;
    nursery) echo "At The Grove Nursery" ;;
  esac
}

op_field() { # op_field <field> -> value or empty
  op read "op://$VAULT/$ITEM/$1" 2>/dev/null || true
}

for T in "${tenants[@]}"; do
  URL=$(url_for "$T")
  echo "== $T ($URL)"

  # -- 1. Setup state ---------------------------------------------------------
  SETUP_DONE=$(curl -fsS --max-time 15 "$URL/ghost/api/admin/authentication/setup/" | jq -r '.setup[0].status')

  PW=$(op_field "ghost_admin_password_$T")
  if [ "$SETUP_DONE" = "false" ]; then
    PW=$(openssl rand -base64 18)
    curl -fsS --max-time 30 -X POST "$URL/ghost/api/admin/authentication/setup/" \
      -H "Content-Type: application/json" -H "Origin: $URL" \
      -d "$(jq -n --arg n "$OWNER_NAME" --arg e "$OWNER_EMAIL" --arg p "$PW" --arg t "$(title_for "$T")" \
            '{setup:[{name:$n,email:$e,password:$p,blogTitle:$t}]}')" > /dev/null
    echo "   owner claimed ($OWNER_EMAIL)"
  else
    if [ -z "$PW" ]; then
      echo "   ERROR: already claimed but no ghost_admin_password_$T in 1Password - investigate who owns it" >&2
      exit 1
    fi
    echo "   already claimed (password on file)"
  fi

  # -- 2. Session --------------------------------------------------------------
  JAR=$(mktemp)
  trap 'rm -f "$JAR"' EXIT
  curl -fsS --max-time 30 -c "$JAR" -X POST "$URL/ghost/api/admin/session/" \
    -H "Content-Type: application/json" -H "Origin: $URL" \
    -d "$(jq -n --arg u "$OWNER_EMAIL" --arg p "$PW" '{username:$u,password:$p}')" > /dev/null
  echo "   session ok"

  # -- 3. Ops integration ------------------------------------------------------
  KEYS=$(curl -fsS --max-time 30 -b "$JAR" -H "Origin: $URL" \
    "$URL/ghost/api/admin/integrations/?include=api_keys&limit=all" \
    | jq --arg n "$INTEGRATION_NAME" '[.integrations[] | select(.name==$n)] | first')
  if [ "$KEYS" = "null" ]; then
    KEYS=$(curl -fsS --max-time 30 -b "$JAR" -X POST "$URL/ghost/api/admin/integrations/?include=api_keys" \
      -H "Content-Type: application/json" -H "Origin: $URL" \
      -d "$(jq -n --arg n "$INTEGRATION_NAME" '{integrations:[{name:$n}]}')" \
      | jq '.integrations[0]')
    echo "   integration created"
  else
    echo "   integration exists"
  fi
  ADMIN_KEY=$(jq -r '.api_keys[] | select(.type=="admin") | .id + ":" + .secret' <<<"$KEYS")
  CONTENT_KEY=$(jq -r '.api_keys[] | select(.type=="content") | .secret' <<<"$KEYS")

  # -- 4. Push to 1Password -----------------------------------------------------
  op item edit "$ITEM" --vault "$VAULT" \
    "ghost_admin_password_${T}[concealed]=$PW" \
    "ghost_admin_key_${T}[concealed]=$ADMIN_KEY" \
    "ghost_content_key_${T}[concealed]=$CONTENT_KEY" > /dev/null
  echo "   1Password updated: ghost_admin_password_$T, ghost_admin_key_$T, ghost_content_key_$T"

  rm -f "$JAR"; trap - EXIT
done

echo "done."
