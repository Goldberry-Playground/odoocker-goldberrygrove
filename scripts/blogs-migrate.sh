#!/usr/bin/env bash
# Migrate one tenant's Ghost content + members from the OLD (snowflake) blog
# to the NEW consolidated blogs droplet - via the public Admin APIs.
#
#   export:  content JSON (GET /db/) + members CSV (GET /members/export/)
#            from the old blog, saved under ./blogs-migration/<tenant>/
#   import:  POST the content JSON into the new blog.* instance. Members are
#            NOT imported: option C (2026-10-19) makes Odoo Farm News the
#            list of record and forbids member writes into any Ghost
#            (GOL-3454). members.csv is kept as a backup only.
#   verify:  post/page/tag/member counts old vs new, printed as a table
#
# What this CANNOT move: content/images + themes ride the filesystem, not the
# export JSON. After import, run the rsync printed at the end (needs SSH to
# both droplets), then re-run `verify`.
#
# goldberry: the apex now serves the Next.js storefront, so the legacy Ghost
# droplet is reached by IP (--resolve) with -k: its Let's Encrypt cert expired
# 2025-10-07 once DNS moved and renewal failed. -k alone would hand the old
# owner password to any substitute cert, so the legacy cert's public key is
# pinned (--pinnedpubkey; curl enforces it even with -k). The pin comes from
# GOLDBERRY_LEGACY_PIN, derived over SSH so the trust anchor is the droplet's
# SSH host key, not the network. New-side calls stay strict.
#
#   export GOLDBERRY_LEGACY_PIN=$(ssh root@178.128.152.218 \
#     'openssl s_client -connect 127.0.0.1:443 -servername goldberrygrove.farm </dev/null 2>/dev/null \
#      | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der \
#      | openssl dgst -sha256 -binary | base64')
#
# Auth: session cookies both sides.
#   NEW side: owner creds from 1P (ghost_admin_password_<tenant>, created by
#             scripts/blogs-bootstrap.sh; owner email below).
#   OLD side: 1P fields ghost_admin_email_<tenant>_old +
#             ghost_admin_password_<tenant>_old (add these once).
#
# Usage:
#   scripts/blogs-migrate.sh <hub|goldberry> export     # pull from old blog
#   scripts/blogs-migrate.sh <hub|goldberry> import     # push into new blog
#   scripts/blogs-migrate.sh <hub|goldberry> verify     # counts, both sides
#   scripts/blogs-migrate.sh <hub|goldberry> all        # export+import+verify
# Requires: curl, jq, op (signed in).
set -euo pipefail

VAULT="Goldberry Grove - Admin"
ITEM="Grove Infra"
NEW_OWNER_EMAIL="josh@goldberrygrove.farm"

T="${1:?usage: blogs-migrate.sh <tenant> <export|import|verify|all>}"
ACTION="${2:?usage: blogs-migrate.sh <tenant> <export|import|verify|all>}"

BLOGS_DROPLET="159.203.131.236"   # grove-prod-blogs; SSH needs -i ~/.ssh/grove-qa-admin

# Extra curl args for every OLD-side call. Expanded as ${arr[@]+"${arr[@]}"}
# because macOS /bin/bash 3.2 calls an empty array unbound under set -u.
OLD_CURL_OPTS=()
OLD_HOST=""        # ssh target holding the old Ghost's content/
OLD_CONTENT=""
case "$T" in
  hub)       OLD_URL="https://gatheringatthegrove.com"; NEW_URL="https://blog.gatheringatthegrove.com" ;;
  goldberry) OLD_URL="https://goldberrygrove.farm";     NEW_URL="https://blog.goldberrygrove.farm"
             OLD_HOST="178.128.152.218"; OLD_CONTENT="/var/www/ghost_restored/content"
             if [ "$ACTION" != "import" ]; then
               if [ -z "${GOLDBERRY_LEGACY_PIN:-}" ]; then
                 echo "GOLDBERRY_LEGACY_PIN is not set; refusing to send credentials to the legacy box with -k." >&2
                 echo "Derive it over SSH (see the header of this script):" >&2
                 echo "  export GOLDBERRY_LEGACY_PIN=\$(ssh root@$OLD_HOST 'openssl s_client -connect 127.0.0.1:443 -servername goldberrygrove.farm </dev/null 2>/dev/null | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64')" >&2
                 exit 2
               fi
               OLD_CURL_OPTS=(--resolve "goldberrygrove.farm:443:$OLD_HOST" -k
                              --pinnedpubkey "sha256//$GOLDBERRY_LEGACY_PIN")
             fi ;;
  ggg)       OLD_URL="";                                NEW_URL="https://blog.woodworkingeorge.com" ;;
  nursery)   OLD_URL="";                                NEW_URL="https://blog.atthegrovenursery.com" ;;
  *) echo "unknown tenant: $T" >&2; exit 2 ;;
esac

DIR="blogs-migration/$T"
mkdir -p "$DIR"

session() { # session <url> <email> <password> [curl opts...] -> cookie jar path
  local url="$1" email="$2" pw="$3"; shift 3
  local jar; jar=$(mktemp)
  curl -fsS --max-time 30 "$@" -c "$jar" -X POST "$url/ghost/api/admin/session/" \
    -H "Content-Type: application/json" -H "Origin: $url" \
    -d "$(jq -n --arg u "$email" --arg p "$pw" '{username:$u,password:$p}')" > /dev/null
  echo "$jar"
}

counts() { # counts <url> <jar> [curl opts...] -> "posts pages tags members"
  local url="$1" jar="$2" out=""; shift 2
  for RES in posts pages tags members; do
    local n
    n=$(curl -fsS --max-time 30 "$@" -b "$jar" -H "Origin: $url" \
      "$url/ghost/api/admin/$RES/?limit=1" | jq -r '.meta.pagination.total') || n="?"
    out="$out $n"
  done
  echo "$out"
}

old_session() {
  local email pw
  email=$(op read "op://$VAULT/$ITEM/ghost_admin_email_${T}_old")
  pw=$(op read "op://$VAULT/$ITEM/ghost_admin_password_${T}_old")
  session "$OLD_URL" "$email" "$pw" ${OLD_CURL_OPTS[@]+"${OLD_CURL_OPTS[@]}"}
}

new_session() {
  local pw
  pw=$(op read "op://$VAULT/$ITEM/ghost_admin_password_${T}")
  session "$NEW_URL" "$NEW_OWNER_EMAIL" "$pw"
}

do_export() {
  [ -n "$OLD_URL" ] || { echo "tenant $T has no old blog to export from" >&2; exit 2; }
  local jar; jar=$(old_session)
  echo "-- exporting from $OLD_URL"
  curl -fsS --max-time 300 ${OLD_CURL_OPTS[@]+"${OLD_CURL_OPTS[@]}"} -b "$jar" -H "Origin: $OLD_URL" \
    "$OLD_URL/ghost/api/admin/db/" > "$DIR/export.json"
  jq -e '.db[0].data.posts | length' "$DIR/export.json" > /dev/null \
    || { echo "export.json doesn't look like a Ghost export" >&2; exit 1; }
  curl -fsS --max-time 300 ${OLD_CURL_OPTS[@]+"${OLD_CURL_OPTS[@]}"} -b "$jar" -H "Origin: $OLD_URL" \
    "$OLD_URL/ghost/api/admin/members/export/" > "$DIR/members.csv"
  echo "   $(jq '.db[0].data.posts | length' "$DIR/export.json") posts, $(($(wc -l < "$DIR/members.csv") - 1)) members -> $DIR/"
  rm -f "$jar"
}

do_import() {
  [ -s "$DIR/export.json" ] || { echo "no $DIR/export.json - run export first" >&2; exit 1; }
  local jar; jar=$(new_session)
  echo "-- importing into $NEW_URL"
  curl -fsS --max-time 600 -b "$jar" -X POST "$NEW_URL/ghost/api/admin/db/" \
    -H "Origin: $NEW_URL" -F "importfile=@$DIR/export.json;type=application/json" \
    | jq -r '.problems // [] | length | "   content import ok (\(.) problems logged)"'
  echo "   members NOT imported (option C: Odoo Farm News is the list of record, GOL-3454); backup at $DIR/members.csv"
  rm -f "$jar"
}

do_verify() {
  local newjar; newjar=$(new_session)
  local newc; newc=$(counts "$NEW_URL" "$newjar"); rm -f "$newjar"
  local oldc="- - - -"
  if [ -n "$OLD_URL" ]; then
    local oldjar; oldjar=$(old_session)
    oldc=$(counts "$OLD_URL" "$oldjar" ${OLD_CURL_OPTS[@]+"${OLD_CURL_OPTS[@]}"}); rm -f "$oldjar"
  fi
  printf '%-10s %8s %8s %8s %8s\n' "" posts pages tags members
  # shellcheck disable=SC2086  # word splitting of the 4 counts is intentional
  printf '%-10s %8s %8s %8s %8s\n' "old" $oldc
  # shellcheck disable=SC2086
  printf '%-10s %8s %8s %8s %8s\n' "new" $newc
  echo
  if [ -z "$OLD_HOST" ]; then
    [ -n "$OLD_URL" ] || return 0
    echo "images/themes do not ride the export - if not yet synced, run (from operator machine):"
    echo "  rsync -az root@<old-droplet>:<ghost-root>/content/images/ ->"
    echo "  root@$BLOGS_DROPLET:/mnt/blogs-data/ghost-$T/images/ (then chown -R 1000:1000 + restart container)"
    return 0
  fi
  echo "images/themes do not ride the export - if not yet synced, relay them through"
  echo "the operator machine (no SSH trust between droplets; no --delete):"
  echo "  rsync -az root@$OLD_HOST:$OLD_CONTENT/images/ ./$DIR/images/"
  echo "  rsync -az -e \"ssh -i ~/.ssh/grove-qa-admin\" ./$DIR/images/ root@$BLOGS_DROPLET:/mnt/blogs-data/ghost-$T/images/"
  echo "  ssh -i ~/.ssh/grove-qa-admin root@$BLOGS_DROPLET 'chown -R 1000:1000 /mnt/blogs-data/ghost-$T/images'"
  echo "  then restart the ghost-$T container"
}

case "$ACTION" in
  export) do_export ;;
  import) do_import ;;
  verify) do_verify ;;
  all) do_export; do_import; do_verify ;;
  *) echo "unknown action: $ACTION" >&2; exit 2 ;;
esac
