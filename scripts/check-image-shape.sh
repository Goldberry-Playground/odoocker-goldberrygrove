#!/usr/bin/env bash
# check-image-shape.sh — Verify Grove images contain the files / behaviors
# we depend on, BEFORE qa-deploy hands them to a live droplet.
#
# Background: PR #90 took ~2 hours to diagnose because the grove-odoo image
# successfully built + pushed + pulled, but it didn't actually contain our
# custom entrypoint.sh (the Dockerfile was missing a COPY line). docker-odoo's
# CI smoke test boots `odoo --stop-after-init` which exercises NONE of the
# entrypoint's orchestration -- so the regression sailed past CI and got
# caught only by SSH-md5-diff on the running container.
#
# This script asserts known invariants ABOUT THE IMAGE BITS, not "did it
# build." Run from qa-deploy.yml as the last preflight before TF apply.
#
# What it checks:
#   grove-odoo:
#     - /entrypoint.sh contains "odoorc.sh"           (PR #90 regression)
#     - /entrypoint.sh contains "APP_ENV"             (PR #89 regression)
#     - /entrypoint.sh has `: ${HOST:=`               (PR #91 regression)
#     - /odoorc.sh contains "done < <(env)"           (PR #89 fix present)
#   grove-caddy:
#     - `caddy list-modules` contains dns.providers.digitalocean
#     - `caddy version` exits 0
#
# Usage:
#   bash scripts/check-image-shape.sh                            # defaults
#   ODOO_IMAGE=ghcr.io/foo/grove-odoo:abc bash scripts/check-image-shape.sh
#
# Env:
#   ODOO_IMAGE      Full image ref (default: ghcr.io/goldberry-playground/grove-odoo:latest)
#   CADDY_IMAGE     Full image ref (default: ghcr.io/goldberry-playground/grove-caddy:latest)
#
# Exit codes:
#   0  All assertions passed
#   1  At least one assertion failed; output names the failing image + check

set -euo pipefail

ODOO_IMAGE="${ODOO_IMAGE:-ghcr.io/goldberry-playground/grove-odoo:latest}"
CADDY_IMAGE="${CADDY_IMAGE:-ghcr.io/goldberry-playground/grove-caddy:latest}"

fail=0

# Helper: run a command inside the image, fail with a useful message on
# non-zero. Wraps `docker run --rm` so we don't leave containers around.
assert_in_image() {
  local image="$1"
  local description="$2"
  shift 2
  if ! docker run --rm "$image" "$@" >/dev/null 2>&1; then
    echo "  ✗ $image: $description"
    fail=1
  else
    echo "  ✓ $image: $description"
  fi
}

echo "── grove-odoo image-shape checks ($ODOO_IMAGE) ──"
docker pull -q "$ODOO_IMAGE" >/dev/null
assert_in_image "$ODOO_IMAGE" \
  "/entrypoint.sh invokes /odoorc.sh (PR #90 fix)" \
  bash -c 'grep -q odoorc.sh /entrypoint.sh'
assert_in_image "$ODOO_IMAGE" \
  "/entrypoint.sh handles APP_ENV (PR #89 fix)" \
  bash -c 'grep -q "APP_ENV" /entrypoint.sh'
assert_in_image "$ODOO_IMAGE" \
  "/entrypoint.sh has HOST/PORT defaults (PR #91 fix)" \
  bash -c 'grep -qE "^: \\\$\\{HOST:=" /entrypoint.sh'
assert_in_image "$ODOO_IMAGE" \
  "/odoorc.sh iterates over full env (PR #89 fix)" \
  bash -c 'grep -q "done < <(env)" /odoorc.sh'

echo
echo "── grove-caddy image-shape checks ($CADDY_IMAGE) ──"
docker pull -q "$CADDY_IMAGE" >/dev/null
assert_in_image "$CADDY_IMAGE" \
  "caddy binary boots" \
  caddy version
assert_in_image "$CADDY_IMAGE" \
  "caddy-dns/digitalocean module is loaded (xcaddy build worked)" \
  sh -c 'caddy list-modules | grep -q dns.providers.digitalocean'

echo
if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF
ERROR: one or more image-shape assertions failed. Do NOT deploy this image to
QA -- it's missing files or behaviors that the runtime needs. Fix the
underlying Dockerfile / script issue and re-trigger the image-rebuild
workflow (docker-odoo.yml / docker-caddy.yml) before re-attempting deploy.

Recovery for the most common failure (PR #90-class missing COPY):
  1. SSH the most recent failed QA droplet AND a known-good local build.
  2. md5 the relevant file (entrypoint.sh, odoorc.sh, Dockerfile) on each.
  3. If they differ, audit the Dockerfile for missing COPY of the source file.
EOF
  exit 1
fi

echo "✓ all image-shape assertions passed"
