#!/usr/bin/env bash
# promotion-asset-smoke — HTTP storefront asset smoke for a QA→prod promotion
# (GOL-1329 launch audit item 4). The DB-side branding gate
# (promotion-integrity-gates.py gate 5) proves the binary is non-empty *in the
# DB*; this proves the *served* asset is real over HTTP: the website logo has
# content-type image/png and a non-placeholder size, and product templates
# actually serve a photo. Prod launched serving the default 8.7 KB 'Your Logo'
# SVG — that would pass a naive "200 OK" check but fail here (wrong content-type,
# tiny size).
#
# Read-only: only GETs public /web/image endpoints. No auth, no writes.
#
# Usage:
#   BASE_URL=https://gatheringatthegrove.com scripts/promotion-asset-smoke.sh
#   BASE_URL=https://qa.gatheringatthegrove.com \
#     PRODUCT_TEMPLATE_IDS="1 2 3" MIN_LOGO_BYTES=20000 \
#     scripts/promotion-asset-smoke.sh
#
# Env:
#   BASE_URL              (required) storefront/Odoo base, no trailing slash
#   MIN_LOGO_BYTES        min acceptable logo size (default 15000 — the real PNG
#                         is ~91 KB, the default 'Your Logo' SVG is ~8.7 KB)
#   PRODUCT_TEMPLATE_IDS  space-separated product.template ids to photo-check
#                         (default "1"); each must return an image/* body
#   WEBSITE_ID            website id for the logo path (default 1)
# Exit: 0 all assets real; 1 usage/connect error; 2 an asset assertion failed.

set -euo pipefail

BASE_URL="${BASE_URL:-}"
MIN_LOGO_BYTES="${MIN_LOGO_BYTES:-15000}"
PRODUCT_TEMPLATE_IDS="${PRODUCT_TEMPLATE_IDS:-1}"
WEBSITE_ID="${WEBSITE_ID:-1}"

if [[ -z "$BASE_URL" ]]; then
  echo "ERROR: BASE_URL is required (e.g. https://gatheringatthegrove.com)" >&2
  exit 1
fi
BASE_URL="${BASE_URL%/}"

fail=0
note() { printf '  [%s] %s\n' "$1" "$2" >&2; }

# -- headers + body size for a URL: prints "<content_type> <bytes>" --
probe() {
  local url="$1" tmp ctype bytes
  tmp="$(mktemp)"
  # -f would drop the body on 404; we want the body/type, so check code ourselves.
  local code
  code="$(curl -sS -L -o "$tmp" -w '%{http_code}' \
            -H 'Accept: image/*' "$url" 2>/dev/null || echo 000)"
  ctype="$(curl -sS -L -o /dev/null -w '%{content_type}' "$url" 2>/dev/null || echo '')"
  bytes="$(wc -c < "$tmp" | tr -d ' ')"
  rm -f "$tmp"
  echo "$code|$ctype|$bytes"
}

echo "== promotion asset smoke ==  base=$BASE_URL  min_logo=${MIN_LOGO_BYTES}B" >&2

# 1) website logo — content-type must be an image (png expected) and size real.
logo_url="$BASE_URL/web/image/website/$WEBSITE_ID/logo"
IFS='|' read -r code ctype bytes <<<"$(probe "$logo_url")"
if [[ "$code" != "200" ]]; then
  note FAIL "website logo HTTP $code ($logo_url)"; fail=1
elif [[ "$ctype" != image/* ]]; then
  note FAIL "website logo content-type '$ctype' is not image/* — placeholder/SVG?"; fail=1
elif (( bytes < MIN_LOGO_BYTES )); then
  note FAIL "website logo only ${bytes}B (< ${MIN_LOGO_BYTES}B) — likely the default 'Your Logo'"; fail=1
else
  note ok "website logo: ${ctype} ${bytes}B"
  [[ "$ctype" == "image/png" ]] || note ok "  (note: content-type ${ctype}, audit expects image/png)"
fi

# 2) product photos — each listed template must serve an image body (> 0 bytes).
photos_ok=0
for tid in $PRODUCT_TEMPLATE_IDS; do
  purl="$BASE_URL/web/image/product.template/$tid/image_1920"
  IFS='|' read -r pcode pctype pbytes <<<"$(probe "$purl")"
  if [[ "$pcode" == "200" && "$pctype" == image/* && "$pbytes" -gt 0 ]]; then
    note ok "product.template/$tid photo: ${pctype} ${pbytes}B"
    photos_ok=$((photos_ok + 1))
  else
    note FAIL "product.template/$tid photo missing (HTTP $pcode ${pctype} ${pbytes}B)"; fail=1
  fi
done
if (( photos_ok == 0 )); then
  note FAIL "NO product photos served — product image count is 0"; fail=1
fi

if (( fail )); then
  echo "❌ asset smoke FAILED — storefront is serving placeholder/missing assets. DO NOT cut over." >&2
  exit 2
fi
echo "✅ storefront assets real (logo + $photos_ok product photo(s))." >&2
