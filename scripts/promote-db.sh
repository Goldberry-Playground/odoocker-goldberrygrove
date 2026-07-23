#!/usr/bin/env bash
# Grove — DB promotion bundler + restorer (filestore moves WITH the DB).
#
# WHY (2026-07-23 QA asset outage, GOL-744 / parent GOL-742)
# ---------------------------------------------------------
# QA was promoted from a local Goldberry instance by dumping the DB but NOT its
# filestore. 676 file-backed attachments, 47 files on disk → every asset bundle,
# image and icon 500'd. The fix is structural, not procedural: the filestore is
# packaged in the SAME bundle as the pg_dump, and the restore side refuses to
# finish if the attachment invariant is breached. "Copy the filestore too" is no
# longer a runbook footnote you can forget — it is a step the tool performs.
#
# The freeze → promote → verify ordering this enforces is documented in
# docs/RUNBOOK-db-promotion-cutover.md. Content-addressed files are
# instance-portable, so a straight copy/tar is correct across instances.
#
# SUBCOMMANDS
# -----------
#   bundle   Freeze source (optional), dump DB + tar filestore + write manifest.
#   restore  Load bundle into target DB, extract filestore, run the fail-loud
#            attachment invariant. Non-zero exit / abort on mismatch.
#
# USAGE
#   promote-db.sh bundle  --db <db> --filestore <dir> --out <bundle-dir> \
#                         [--freeze-cmd "<cmd>"] [--unfreeze-cmd "<cmd>"]
#   promote-db.sh restore --db <db> --filestore <dir> --in  <bundle-dir> \
#                         [--owner <uid:gid>] [--tolerance N]
#
#   --filestore  Path to THIS db's filestore dir (holds the 2-char shard dirs),
#                e.g. /var/lib/odoo/filestore/<db>.
#   --freeze-cmd Command that stops writes on the source (e.g. stop the Odoo
#                container, or set the DB to read-only). STRONGLY recommended:
#                without a freeze, attachments written mid-dump land in the DB
#                but not the tar, tripping the invariant. See runbook §Freeze.
#   --owner      chown the extracted filestore to uid:gid. Odoo's `odoo` user is
#                100:101 on the official odoo:19 image — resolve from the image,
#                never hardcode blindly (see runbook + CLAUDE.md deploy invariants).
#
# ENV (dump/restore plumbing — override for containerised Postgres)
#   PG_DUMP  pg_dump prefix. Default "pg_dump". Containerised source:
#            PG_DUMP="docker compose exec -T postgres pg_dump -U odoo"
#   PSQL     psql prefix.    Default "psql". Containerised target:
#            PSQL="docker compose exec -T postgres psql -U odoo"

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$HERE/check-attachment-invariant.sh"
PG_DUMP="${PG_DUMP:-pg_dump}"
PSQL="${PSQL:-psql}"

die() { echo "promote-db: $*" >&2; exit 3; }

# --- shared arg parse -------------------------------------------------------
SUB="${1:-}"; shift || true
[[ "$SUB" == "bundle" || "$SUB" == "restore" ]] || die "first arg must be 'bundle' or 'restore'"

DB="" FILESTORE="" OUT="" IN="" FREEZE_CMD="" UNFREEZE_CMD="" OWNER="" TOLERANCE="25"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)           DB="${2:?}"; shift 2 ;;
    --filestore)    FILESTORE="${2:?}"; shift 2 ;;
    --out)          OUT="${2:?}"; shift 2 ;;
    --in)           IN="${2:?}"; shift 2 ;;
    --freeze-cmd)   FREEZE_CMD="${2:?}"; shift 2 ;;
    --unfreeze-cmd) UNFREEZE_CMD="${2:?}"; shift 2 ;;
    --owner)        OWNER="${2:?}"; shift 2 ;;
    --tolerance)    TOLERANCE="${2:?}"; shift 2 ;;
    -h|--help)      sed -n '2,60p' "$0"; exit 0 ;;
    *)              die "unknown arg: $1" ;;
  esac
done
[[ -n "$DB" ]]        || die "--db is required"
[[ -n "$FILESTORE" ]] || die "--filestore is required"

count_expected() { echo "SELECT count(DISTINCT store_fname) FROM ir_attachment WHERE store_fname IS NOT NULL;" | $PSQL -d "$DB" -tAX; }
count_rows()     { echo "SELECT count(*)                     FROM ir_attachment WHERE store_fname IS NOT NULL;" | $PSQL -d "$DB" -tAX; }
count_files()    { [[ -d "$FILESTORE" ]] && find "$FILESTORE" -type f | wc -l | tr -d ' ' || echo 0; }

# --- bundle -----------------------------------------------------------------
if [[ "$SUB" == "bundle" ]]; then
  [[ -n "$OUT" ]] || die "--out is required for bundle"
  [[ -d "$FILESTORE" ]] || die "filestore dir '$FILESTORE' not found — refusing to bundle a DB with no filestore"
  mkdir -p "$OUT"

  if [[ -n "$FREEZE_CMD" ]]; then
    echo "[bundle] FREEZE: $FREEZE_CMD"
    eval "$FREEZE_CMD"
    # Always attempt to unfreeze, even if the dump fails.
    trap '[[ -n "$UNFREEZE_CMD" ]] && { echo "[bundle] UNFREEZE: $UNFREEZE_CMD"; eval "$UNFREEZE_CMD" || true; }' EXIT
  else
    echo "[bundle] ⚠️  no --freeze-cmd given: writes during the dump can desync the DB from the tar and trip the invariant on restore. Proceeding, but a freeze is strongly recommended for a real cutover."
  fi

  echo "[bundle] [1/3] pg_dump → db.sql.zst"
  $PG_DUMP -d "$DB" --format=plain --no-owner --no-privileges | zstd -19 -T0 -o "$OUT/db.sql.zst"

  echo "[bundle] [2/3] tar filestore → filestore.tar.zst (STRUCTURAL — moves with the DB)"
  tar -C "$(dirname "$FILESTORE")" -cf - "$(basename "$FILESTORE")" | zstd -19 -T0 -o "$OUT/filestore.tar.zst"

  echo "[bundle] [3/3] manifest.json (source-side invariant baseline)"
  EXP="$(count_expected)"; ROWS="$(count_rows)"; FILES="$(count_files)"
  # Keep the manifest valid JSON even if a count query returned nothing. The
  # authoritative gate is the TARGET-side live check at restore, not this
  # baseline — but a broken baseline shouldn't emit malformed JSON.
  [[ "$EXP"  =~ ^[0-9]+$ ]] || { echo "[bundle] WARN: expected-count non-numeric ('$EXP') → recording null"; EXP=null; }
  [[ "$ROWS" =~ ^[0-9]+$ ]] || { echo "[bundle] WARN: row-count non-numeric ('$ROWS') → recording null"; ROWS=null; }
  cat > "$OUT/manifest.json" <<EOF
{
  "db": "$DB",
  "filestore_basename": "$(basename "$FILESTORE")",
  "expected_files_distinct_store_fname": $EXP,
  "file_backed_attachment_rows": $ROWS,
  "files_on_disk_at_bundle": $FILES,
  "note": "expected_files == distinct store_fname (content-addressed dedup). Restore must match this on the target within tolerance."
}
EOF
  cat "$OUT/manifest.json"
  echo "[bundle] ✅ done → $OUT (db.sql.zst + filestore.tar.zst + manifest.json)"
  echo "[bundle] restore with: promote-db.sh restore --db <target> --filestore <target-fs> --in $OUT"
  exit 0
fi

# --- restore ----------------------------------------------------------------
if [[ "$SUB" == "restore" ]]; then
  [[ -n "$IN" ]] || die "--in is required for restore"
  [[ -f "$IN/db.sql.zst" ]]        || die "$IN/db.sql.zst not found"
  [[ -f "$IN/filestore.tar.zst" ]] || die "$IN/filestore.tar.zst not found — this bundle has no filestore; the promotion would recreate the 2026-07-23 outage. Aborting."
  [[ -x "$CHECK" ]] || die "invariant check not found/executable at $CHECK"

  echo "[restore] [1/3] load db.sql.zst → $DB"
  zstd -d -c "$IN/db.sql.zst" | $PSQL -d "$DB"

  echo "[restore] [2/3] extract filestore.tar.zst → $FILESTORE"
  mkdir -p "$FILESTORE"
  # tar entries are "<basename>/…"; strip that one leading dir into the target.
  zstd -d -c "$IN/filestore.tar.zst" | tar -xf - -C "$FILESTORE" --strip-components=1
  if [[ -n "$OWNER" ]]; then
    echo "[restore] chown -R $OWNER $FILESTORE"
    chown -R "$OWNER" "$FILESTORE"
  else
    echo "[restore] ⚠️  no --owner: ensure the filestore is owned by the container's odoo user (100:101 on odoo:19 — resolve from the image, don't hardcode). See runbook."
  fi

  echo "[restore] [3/3] fail-loud attachment invariant"
  PSQL="$PSQL" "$CHECK" --db "$DB" --filestore "$FILESTORE" --tolerance "$TOLERANCE" --mode fail
  echo "[restore] ✅ done — invariant holds; safe to bring the stack up / cut over."
  exit 0
fi
