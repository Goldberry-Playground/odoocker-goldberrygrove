#!/usr/bin/env bash
# Migrate one tenant's Ghost content + members from the OLD (snowflake) blog
# to the NEW consolidated blogs droplet - via the public Admin APIs.
#
#   export:  content JSON (GET /db/) + members CSV (GET /members/export/)
#            from the old blog, saved under ./blogs-migration/<tenant>/
#   import:  POST both into the new blog.* instance
#   verify:  post/page/tag/member counts old vs new, printed as a table
#
# What this CANNOT move: content/images + themes ride the filesystem, not the
# export JSON. After import, run the rsync printed at the end (needs SSH to
# the old droplet), then re-run `verify`.
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

case "$T" in
  hub)       OLD_URL="https://gatheringatthegrove.com"; NEW_URL="https://blog.gatheringatthegrove.com" ;;
  goldberry) OLD_URL="https://goldberrygrove.farm";     NEW_URL="https://blog.goldberrygrove.farm" ;;
  ggg)       OLD_URL="";                                NEW_URL="https://blog.woodworkingeorge.com" ;;
  nursery)   OLD_URL="";                                NEW_URL="https://blog.atthegrovenursery.com" ;;
  *) echo "unknown tenant: $T" >&2; exit 2 ;;
esac

DIR="blogs-migration/$T"
mkdir -p "$DIR"

session() { # session <url> <email> <password> -> cookie jar path
  local jar; jar=$(mktemp)
  curl -fsS --max-time 30 -c "$jar" -X POST "$1/ghost/api/admin/session/" \
    -H "Content-Type: application/json" -H "Origin: $1" \
    -d "$(jq -n --arg u "$2" --arg p "$3" '{username:$u,password:$p}')" > /dev/null
  echo "$jar"
}

counts() { # counts <url> <jar> -> "posts pages tags members"
  local url="$1" jar="$2" out=""
  for RES in posts pages tags members; do
    local n
    n=$(curl -fsS --max-time 30 -b "$jar" -H "Origin: $url" \
      "$url/ghost/api/admin/$RES/?limit=1" | jq -r '.meta.pagination.total') || n="?"
    out="$out $n"
  done
  echo "$out"
}

old_session() {
  local email pw
  email=$(op read "op://$VAULT/$ITEM/ghost_admin_email_${T}_old")
  pw=$(op read "op://$VAULT/$ITEM/ghost_admin_password_${T}_old")
  session "$OLD_URL" "$email" "$pw"
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
  curl -fsS --max-time 300 -b "$jar" -H "Origin: $OLD_URL" \
    "$OLD_URL/ghost/api/admin/db/" > "$DIR/export.json"
  jq -e '.db[0].data.posts | length' "$DIR/export.json" > /dev/null \
    || { echo "export.json doesn't look like a Ghost export" >&2; exit 1; }
  curl -fsS --max-time 300 -b "$jar" -H "Origin: $OLD_URL" \
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
  if [ -s "$DIR/members.csv" ] && [ "$(wc -l < "$DIR/members.csv")" -gt 1 ]; then
    curl -fsS --max-time 600 -b "$jar" -X POST "$NEW_URL/ghost/api/admin/members/upload/" \
      -H "Origin: $NEW_URL" -F "membersfile=@$DIR/members.csv;type=text/csv" \
      | jq -r '"   members import: \(.meta.stats.imported // "?") imported, \(.meta.stats.invalid // "?") invalid"'
  else
    echo "   no members to import"
  fi
  rm -f "$jar"
}

do_verify() {
  local newjar; newjar=$(new_session)
  local newc; newc=$(counts "$NEW_URL" "$newjar"); rm -f "$newjar"
  local oldc="- - - -"
  if [ -n "$OLD_URL" ]; then
    local oldjar; oldjar=$(old_session)
    oldc=$(counts "$OLD_URL" "$oldjar"); rm -f "$oldjar"
  fi
  printf '%-10s %8s %8s %8s %8s\n' "" posts pages tags members
  # shellcheck disable=SC2086  # word splitting of the 4 counts is intentional
  printf '%-10s %8s %8s %8s %8s\n' "old" $oldc
  # shellcheck disable=SC2086
  printf '%-10s %8s %8s %8s %8s\n' "new" $newc
  echo
  echo "images/themes do not ride the export - if not yet synced, run (from operator machine):"
  echo "  rsync -az root@<old-droplet>:/var/www/ghost/content/images/ ->"
  echo "  root@<blogs-droplet>:/mnt/blogs-data/ghost-$T/images/ (then chown -R 1000:1000 + restart container)"
}

case "$ACTION" in
  export) do_export ;;
  import) do_import ;;
  verify) do_verify ;;
  all) do_export; do_import; do_verify ;;
  *) echo "unknown action: $ACTION" >&2; exit 2 ;;
esac
