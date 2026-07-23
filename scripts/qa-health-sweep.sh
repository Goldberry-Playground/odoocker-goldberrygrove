#!/usr/bin/env bash
# Synthetic health sweep for the Level 3 QA environment. The successor to
# the monolith's deploy-pipeline alerting: instead of watching pipeline
# runs, watch REALITY -- the 7 public hostnames plus the App Platform
# deployment phases.
#
# Exit 0 = everything healthy; exit 1 = one or more checks degraded (the
# calling workflow's conclusion then feeds discord-status.sh's streak /
# recovery logic).
#
# Env:
#   DIGITALOCEAN_TOKEN   optional -- enables the App Platform phase checks.
#                        Without it, only the HTTP sweep runs.
#   QA_ZONE              default qa.gatheringatthegrove.com
set -uo pipefail

QA_ZONE="${QA_ZONE:-qa.gatheringatthegrove.com}"
fail=0

echo "== HTTP sweep =="
# odoo returns 303 (redirect to /odoo login); any 2xx/3xx counts as
# serving. 000/5xx = degraded. 4xx is deliberate: a 403/404 at the APEX
# of these services would be a routing regression.
# oo/keep are NOT swept: the obs droplet firewall scopes 443 to the
# admin CIDR (Keep runs NO_AUTH -- must never be public), so CI always
# sees 000 and paged DEGRADED every 15min (2026-07-07). Obs-plane
# health is covered by the App Platform phase checks below + the
# OpenObserve monitors themselves (ADR-008).
for host in hub goldberry ggg nursery odoo; do
  code=$(curl -sk -o /dev/null -w "%{http_code}" "https://${host}.${QA_ZONE}/" --connect-timeout 5 --max-time 12 2>/dev/null)
  case "$code" in
    2*|3*) echo "  ok   ${host}.${QA_ZONE} (${code})" ;;
    *)     echo "  FAIL ${host}.${QA_ZONE} (${code})"; fail=1 ;;
  esac
done

echo "== Odoo content probes =="
# Status codes lie. On 2026-07-23 qa-health stayed GREEN for 18h while the
# site was fully unstyled: the asset bundle 404'd into a ~250B error stub
# (HTTP 200) and a logo served HTTP 200 as image/png while its bytes were
# SVG. These checks decode the payload -- size + magic bytes + baked
# compile-error strings -- so a lying 200 flips the run to DEGRADED.
# Odoo assets live on the odoo host only (frontends are Next.js); same
# connect/max-time bounds as the sweep above so a slow host can't false-fail.
ODOO_HOST="odoo.${QA_ZONE}"

# --- 1 + 3: asset bundle (size floor + baked compile error) ---
# Pull /web/login, extract one /web/assets/*.css URL, fetch it. A real
# frontend bundle is hundreds of KB; an error stub is a few hundred bytes.
login_html=$(curl -sk "https://${ODOO_HOST}/web/login" --connect-timeout 5 --max-time 12 2>/dev/null)
css_path=$(printf '%s' "$login_html" | grep -oE '/web/assets/[^"'"'"'?]+\.css' | head -1)
if [ -z "$css_path" ]; then
  echo "  FAIL ${ODOO_HOST} asset-bundle: no /web/assets/*.css URL in /web/login"; fail=1
else
  bundle=$(mktemp)
  css_code=$(curl -sk -o "$bundle" -w '%{http_code}' \
    "https://${ODOO_HOST}${css_path}" --connect-timeout 5 --max-time 12 2>/dev/null)
  size=$(wc -c < "$bundle" | tr -d ' ')
  if [ "$css_code" != "200" ]; then
    echo "  FAIL ${ODOO_HOST} asset-bundle ${css_path}: HTTP ${css_code}"; fail=1
  elif [ "${size:-0}" -le 51200 ]; then
    echo "  FAIL ${ODOO_HOST} asset-bundle ${css_path}: ${size}B (<=50KB, likely an error stub)"; fail=1
  else
    echo "  ok   ${ODOO_HOST} asset-bundle ${css_path}: ${size}B"
  fi
  # Odoo bakes SCSS compile errors into the STORED bundle -- present even
  # when the bundle is large and served 200.
  if grep -q 'Could not get content' "$bundle" 2>/dev/null; then
    echo "  FAIL ${ODOO_HOST} asset-bundle ${css_path}: baked compile error ('Could not get content')"; fail=1
  fi
  rm -f "$bundle"
fi

# --- 2: attachment-backed image (declared content-type vs magic bytes) ---
img=$(mktemp)
img_url="https://${ODOO_HOST}/web/image/website/1/logo"
read -r img_code img_ct < <(curl -sk -o "$img" -w '%{http_code} %{content_type}' \
  "$img_url" --connect-timeout 5 --max-time 12 2>/dev/null)
img_ct="${img_ct%%;*}"            # drop "; charset=..."
img_ct="${img_ct// /}"
sniff=$(python3 - "$img" <<'PY'
import sys
d = open(sys.argv[1], 'rb').read(1024)
s = d.lstrip()
if   d[:8] == b'\x89PNG\r\n\x1a\n':                    print('image/png')
elif d[:3] == b'\xff\xd8\xff':                         print('image/jpeg')
elif d[:6] in (b'GIF87a', b'GIF89a'):                  print('image/gif')
elif d[:4] == b'RIFF' and d[8:12] == b'WEBP':          print('image/webp')
elif s[:4].lower() == b'<svg' or (s[:5].lower() == b'<?xml' and b'<svg' in d.lower()):
    print('image/svg+xml')
else:                                                  print('unknown')
PY
)
if [ "$img_code" != "200" ]; then
  echo "  FAIL ${ODOO_HOST} attachment-image /web/image/website/1/logo: HTTP ${img_code}"; fail=1
elif [ "$sniff" = "unknown" ]; then
  echo "  FAIL ${ODOO_HOST} attachment-image: undecodable bytes (declared ${img_ct:-none})"; fail=1
elif [ "$sniff" != "$img_ct" ]; then
  echo "  FAIL ${ODOO_HOST} attachment-image: declared ${img_ct:-none} but bytes are ${sniff}"; fail=1
else
  echo "  ok   ${ODOO_HOST} attachment-image: ${sniff}"
fi
rm -f "$img"

if [ -n "${DIGITALOCEAN_TOKEN:-}" ]; then
  echo "== App Platform deployment phases =="
  resp=$(curl -s --max-time 20 -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
    "https://api.digitalocean.com/v2/apps?per_page=50" || true)
  if [ -z "$resp" ]; then
    echo "  FAIL could not list apps (API unreachable)"; fail=1
  else
    # Evaluate every grove-*-qa app: ACTIVE is healthy; a stuck/errored
    # deployment is degraded even if yesterday's deployment still serves
    # (that's exactly the state that silently rots).
    out=$(printf '%s' "$resp" | python3 -c '
import sys, json
d = json.load(sys.stdin)
bad = 0
seen = 0
for app in d.get("apps", []):
    name = app["spec"]["name"]
    if not (name.startswith("grove-") and name.endswith("-qa")):
        continue
    seen += 1
    dep = app.get("in_progress_deployment") or app.get("active_deployment") or {}
    phase = dep.get("phase", "UNKNOWN")
    status = "ok  " if phase in ("ACTIVE", "DEPLOYING", "BUILDING", "PENDING_DEPLOY") else "FAIL"
    if status == "FAIL":
        bad = 1
    print(f"  {status} {name}: {phase}")
if seen < 4:
    print(f"  FAIL expected 4 grove-*-qa apps, found {seen}")
    bad = 1
sys.exit(bad)')
    rc=$?
    echo "$out"
    [ "$rc" != "0" ] && fail=1
  fi
else
  echo "== App Platform checks skipped (no DIGITALOCEAN_TOKEN) =="
fi

if [ "$fail" = "0" ]; then
  echo "RESULT: healthy"
else
  echo "RESULT: DEGRADED"
fi
exit $fail
