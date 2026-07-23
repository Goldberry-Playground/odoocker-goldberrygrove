#!/usr/bin/env bash
# Grove — post-restore attachment invariant (fail-loud).
#
# WHY THIS EXISTS (2026-07-23 QA asset outage, GOL-744 / parent GOL-742)
# --------------------------------------------------------------------
# QA was promoted from a local Goldberry Odoo instance *without* its filestore:
# 676 file-backed `ir_attachment` rows resolved to only 47 files on disk. Every
# attachment-served resource then 500'd — all CSS/JS asset bundles (fully
# unstyled site), logo, favicon, 452 payment-method icons, 35 partner images.
# Nothing failed loudly at promotion time; the miss only surfaced as browser
# 500s after cutover. This check turns that silent miss into a loud abort BEFORE
# traffic is switched over.
#
# THE INVARIANT
# -------------
# Odoo's filestore is content-addressed: an attachment's bytes live at
# `filestore/<db>/<2-char>/<sha1-of-content>` and `ir_attachment.store_fname`
# holds the `<2-char>/<sha1>` path. Because the store is content-addressed,
# MANY attachment rows can dedup onto ONE file — so the honest "expected files"
# figure is the count of DISTINCT `store_fname`, not the raw row count. That
# distinct count must match the number of files actually on disk:
#
#     count(DISTINCT store_fname WHERE store_fname IS NOT NULL)  ≈  find <fs> -type f
#
# A small delta is tolerated for attachments written during the freeze window.
# The dangerous direction is DB-expects-MORE-than-disk-has (missing bytes → the
# 500s we hit); excess files on disk are inert orphans (they 404 nothing) and
# only warn. See docs/RUNBOOK-db-promotion-cutover.md.
#
# USAGE
# -----
#   check-attachment-invariant.sh --db <db> --filestore <dir> \
#       [--tolerance N] [--mode fail|warn]
#
#   --filestore   Path to THIS db's filestore dir (the dir holding the 2-char
#                 shard subdirs), e.g. /var/lib/odoo/filestore/<db> on the box,
#                 or the extracted preview volume subdir.
#   --tolerance   Max allowed (expected_files - files_on_disk). Default 25.
#   --mode        fail (default) → non-zero exit / abort on breach.
#                 warn           → log loudly, exit 0 (ephemeral envs).
#
# ENV
#   PSQL   psql invocation prefix. Default "psql". For a containerised PG:
#          PSQL="docker compose exec -T postgres psql -U odoo"
#
# EXIT CODES
#   0  invariant holds (or breached in --mode warn)
#   2  invariant BREACHED (missing bytes) in --mode fail
#   3  usage / precondition error

set -euo pipefail

DB=""
FILESTORE=""
TOLERANCE=25
MODE="fail"
PSQL="${PSQL:-psql}"

die() { echo "check-attachment-invariant: $*" >&2; exit 3; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)         DB="${2:?}"; shift 2 ;;
    --filestore)  FILESTORE="${2:?}"; shift 2 ;;
    --tolerance)  TOLERANCE="${2:?}"; shift 2 ;;
    --mode)       MODE="${2:?}"; shift 2 ;;
    -h|--help)    sed -n '2,50p' "$0"; exit 0 ;;
    *)            die "unknown arg: $1" ;;
  esac
done

[[ -n "$DB" ]]        || die "--db is required"
[[ -n "$FILESTORE" ]] || die "--filestore is required"
[[ "$MODE" == "fail" || "$MODE" == "warn" ]] || die "--mode must be fail|warn"
[[ "$TOLERANCE" =~ ^[0-9]+$ ]] || die "--tolerance must be a non-negative integer"

if [[ ! -d "$FILESTORE" ]]; then
  # An entirely missing filestore dir IS the incident. Treat as zero files so
  # the breach math runs and fails loud, rather than erroring out ambiguously.
  echo "[invariant] WARNING: filestore dir '$FILESTORE' does not exist — treating as 0 files" >&2
fi

# DISTINCT store_fname = the number of unique files the DB expects on disk.
# Raw row count is reported alongside for context (dedup makes it >= distinct).
sql() { echo "$1" | $PSQL -d "$DB" -tAX; }

EXPECTED_FILES="$(sql "SELECT count(DISTINCT store_fname) FROM ir_attachment WHERE store_fname IS NOT NULL;")"
TOTAL_ROWS="$(sql    "SELECT count(*)                     FROM ir_attachment WHERE store_fname IS NOT NULL;")"

# A safety gate must never PASS because the query silently failed: an empty /
# non-numeric result would arithmetic-coerce to 0 and mask a real breach.
[[ "$EXPECTED_FILES" =~ ^[0-9]+$ ]] || die "psql returned non-numeric expected-count ('$EXPECTED_FILES') — cannot verify invariant (is PSQL/-d/DB correct?)"
[[ "$TOTAL_ROWS"     =~ ^[0-9]+$ ]] || die "psql returned non-numeric row-count ('$TOTAL_ROWS') — cannot verify invariant"

# Files actually on disk. -type f counts only the content-addressed blobs.
if [[ -d "$FILESTORE" ]]; then
  FILES_ON_DISK="$(find "$FILESTORE" -type f | wc -l | tr -d ' ')"
else
  FILES_ON_DISK=0
fi

MISSING=$(( EXPECTED_FILES - FILES_ON_DISK ))   # >0 = DB expects bytes not on disk (dangerous)
ORPHANS=$(( FILES_ON_DISK - EXPECTED_FILES ))   # >0 = extra files on disk (inert)

echo "[invariant] db=$DB filestore=$FILESTORE"
echo "[invariant] file-backed ir_attachment rows : $TOTAL_ROWS"
echo "[invariant] distinct store_fname (expected): $EXPECTED_FILES"
echo "[invariant] files on disk                  : $FILES_ON_DISK"
echo "[invariant] missing (expected-disk)=$MISSING  orphans (disk-expected)=$ORPHANS  tolerance=$TOLERANCE"

if (( ORPHANS > TOLERANCE )); then
  echo "[invariant] NOTE: $ORPHANS more files on disk than the DB references — inert orphans (harmless), not a promotion miss." >&2
fi

if (( MISSING > TOLERANCE )); then
  MSG="ATTACHMENT INVARIANT BREACHED: DB expects $EXPECTED_FILES files but only $FILES_ON_DISK are on disk ($MISSING missing, tolerance $TOLERANCE). The filestore did NOT move with the DB — attachment-served resources (asset bundles, images, icons) will 500. This is the 2026-07-23 outage. DO NOT CUT OVER."
  if [[ "$MODE" == "fail" ]]; then
    echo "[invariant] 🚨 $MSG" >&2
    exit 2
  else
    echo "[invariant] ⚠️  $MSG" >&2
    exit 0
  fi
fi

echo "[invariant] ✅ OK — filestore matches the DB within tolerance."
