#!/usr/bin/env bash
# Grove Preview — bootstrap SEED snapshot packager + uploader.
#
# Purpose
# -------
# The nightly `sanitize-and-upload.sh` pipeline dumps the PROD Odoo Postgres,
# sanitizes it, and uploads snapshot + filestore + heartbeat to the
# `grove-preview-data` Space. That pipeline only runs once the prod droplet
# exists (it is a systemd timer installed on prod — see docs install-systemd.md).
#
# Until prod is live, the bucket is empty, so `preview-up` (grove-sites) fails
# its "Read snapshot heartbeat" gate and never reaches `terraform apply`
# (GOL-310 / blocks GOL-6 live acceptance).
#
# This script produces a *bootstrap* seed from a freshly-initialised base
# `grove_preview` DB (no prod data, so nothing to leak) so the preview pipeline
# can be exercised end-to-end today. It is intentionally the same shape as
# `sanitize-and-upload.sh` (same bucket layout, same sanitizer, same heartbeat)
# so preview cloud-init `restore.sh` consumes it unchanged. Once the prod
# nightly cron runs, its fresher snapshot supersedes this seed on the next day.
#
# Contract with preview cloud-init restore.sh:
#   s3://$BUCKET/snapshots/prod-sanitized-<DATE>.sql.zst   plain pg_dump, zstd
#   s3://$BUCKET/filestore/prod-sanitized-<DATE>.tar.zst   tar of filestore/, zstd
#   s3://$BUCKET/heartbeat.txt                             <DATE>
#
# Env:
#   Provide the source dump ONE of two ways:
#     (a) DUMP_FILE=/path/to/plain.sql            — a pre-produced plain pg_dump
#         (use this when the server is newer than the runner's pg_dump, e.g. CI
#          dumps inside the postgres:17 container to avoid a client mismatch);
#     (b) PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE — dump a live DB directly
#         (the prod nightly path).
#   SPACES_ACCESS_KEY, SPACES_SECRET_KEY            — grove-preview-data RW creds
#   SPACES_ENDPOINT   (default https://nyc3.digitaloceanspaces.com)
#   SPACES_BUCKET     (default grove-preview-data)
#   SANITIZER         (default <repo>/scripts/preview/sanitize_dump.py)
#
# Requires on PATH: python3, zstd, tar, aws (+ pg_dump for path (b)).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${SPACES_ACCESS_KEY:?}" "${SPACES_SECRET_KEY:?}"
PGPORT="${PGPORT:-5432}"
SPACES_ENDPOINT="${SPACES_ENDPOINT:-https://nyc3.digitaloceanspaces.com}"
SPACES_BUCKET="${SPACES_BUCKET:-grove-preview-data}"
SANITIZER="${SANITIZER:-$HERE/sanitize_dump.py}"
DUMP_FILE="${DUMP_FILE:-}"

DATE="$(date -u +%Y-%m-%d)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

s3() {
  AWS_ACCESS_KEY_ID="$SPACES_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$SPACES_SECRET_KEY" \
    aws --endpoint-url "$SPACES_ENDPOINT" s3 "$@"
}

echo "[1/5] source dump -> sanitize -> zstd"
if [[ -n "$DUMP_FILE" ]]; then
  cat "$DUMP_FILE"
else
  : "${PGHOST:?need DUMP_FILE or PGHOST}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
  PGPASSWORD="$PGPASSWORD" pg_dump \
    -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
    --format=plain --no-owner --no-privileges
fi \
  | python3 "$SANITIZER" \
  | zstd -19 -T0 -o "snapshot-${DATE}.sql.zst"

echo "[2/5] package (empty) filestore -> zstd"
# base-only DB has no attachments; ship a valid but empty filestore tar so
# restore.sh's `tar -x --strip-components=1` into grove_preview succeeds.
mkdir -p fs/filestore
tar -C fs -cf - filestore | zstd -19 -T0 -o "filestore-${DATE}.tar.zst"

echo "[3/5] upload snapshot"
s3 cp "snapshot-${DATE}.sql.zst" \
  "s3://${SPACES_BUCKET}/snapshots/prod-sanitized-${DATE}.sql.zst" --no-progress

echo "[4/5] upload filestore"
s3 cp "filestore-${DATE}.tar.zst" \
  "s3://${SPACES_BUCKET}/filestore/prod-sanitized-${DATE}.tar.zst" --no-progress

echo "[5/5] heartbeat (written LAST, only after snapshot+filestore are up)"
printf '%s' "$DATE" > heartbeat.txt
s3 cp heartbeat.txt "s3://${SPACES_BUCKET}/heartbeat.txt" --no-progress

echo "grove-preview-seed OK ${DATE}"
echo "  s3://${SPACES_BUCKET}/snapshots/prod-sanitized-${DATE}.sql.zst"
echo "  s3://${SPACES_BUCKET}/filestore/prod-sanitized-${DATE}.tar.zst"
echo "  s3://${SPACES_BUCKET}/heartbeat.txt = ${DATE}"
