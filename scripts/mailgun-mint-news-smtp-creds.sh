#!/usr/bin/env bash
# mailgun-mint-news-smtp-creds.sh — mint the four news.<domain> Mailgun SMTP
# logins and store them on 1Password (GOL-3455, bulk newsletter sending domains).
#
# WHY A HUMAN RUNS THIS: the ops service account is READ-ONLY on 1Password, so an
# agent can mint Mailgun credentials but cannot store them. This turns Josh's
# part into one command. Everything else (domains, DNS, Odoo rows) is codified.
#
#   scripts/mailgun-mint-news-smtp-creds.sh            # dry-run: shows the plan
#   scripts/mailgun-mint-news-smtp-creds.sh --apply    # mint + store + verify
#   scripts/mailgun-mint-news-smtp-creds.sh --apply --only hub,nursery
#
# What --apply does, per tenant:
#   1. generates a 24-char alphanumeric password (never printed);
#   2. creates the Mailgun SMTP login newsletter@news.<domain>, or resets its
#      password when the login already exists (re-run safe);
#   3. writes smtp_login_news_<tenant> / smtp_password_news_<tenant> onto the 1P
#      item `Mailgun | Goldberry Grove` (Goldberry Grove - Admin);
#   4. re-reads both fields — `op item edit` can print a spurious error even
#      when the write landed, so the re-read is the source of truth;
#   5. SMTP AUTHs against smtp.mailgun.org:587 STARTTLS with the stored values.
#
# Needs: op (signed in with WRITE access to `Goldberry Grove - Admin`), curl,
# python3. The Mailgun API key is read from the same 1P item (mailgun_api_token).
# Nothing secret is echoed; the Mailgun key and passwords travel via files in a
# private temp dir that is removed on exit.
set -euo pipefail

VAULT="Goldberry Grove - Admin"
ITEM="7t2pap33hu37bftpsy7uzmakuy"   # `Mailgun | Goldberry Grove`
MG_API="https://api.mailgun.net"    # US region
SMTP_HOST="smtp.mailgun.org"
SMTP_PORT=587
LOGIN_LOCALPART="newsletter"

# tenant -> bulk sending domain (see docs/RUNBOOK-mailgun-transactional-email.md).
# A case statement rather than an associative array so the script also runs on
# macOS' stock bash 3.2.
domain_for() {
  case "$1" in
    hub)       echo "news.gatheringatthegrove.com" ;;
    goldberry) echo "news.goldberrygrove.farm" ;;
    nursery)   echo "news.atthegrovenursery.com" ;;
    ggg)       echo "news.woodworkingeorge.com" ;;
    *)         echo "" ;;
  esac
}

APPLY=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --only) ONLY="${2:-}"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

for bin in op curl python3; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

tenants=()
if [ -n "$ONLY" ]; then
  IFS=',' read -r -a tenants <<<"$ONLY"
else
  tenants=(hub goldberry nursery ggg)
fi
for t in "${tenants[@]}"; do
  [ -n "$(domain_for "$t")" ] || { echo "unknown tenant: $t (hub|goldberry|nursery|ggg)" >&2; exit 1; }
done

echo "== news.<domain> SMTP credentials (GOL-3455) =="
echo "   1P item: $VAULT / $ITEM   Mailgun: $MG_API   mode: $([ $APPLY -eq 1 ] && echo APPLY || echo DRY-RUN)"
for t in "${tenants[@]}"; do
  echo "   $t: ${LOGIN_LOCALPART}@$(domain_for "$t")  -> smtp_login_news_${t} / smtp_password_news_${t}"
done

# Read the Mailgun API key into a file (never into a shell variable that could be echoed).
op item get "$ITEM" --vault "$VAULT" --fields label=mailgun_api_token --reveal >"$TMP/mgkey" 2>"$TMP/op.err" \
  || { echo "could not read mailgun_api_token from 1P:" >&2; cat "$TMP/op.err" >&2; exit 1; }
tr -d '\r\n' <"$TMP/mgkey" >"$TMP/mgkey.trim"
[ -s "$TMP/mgkey.trim" ] || { echo "mailgun_api_token is EMPTY on the 1P item" >&2; exit 1; }
# curl reads the credential from a config file so it never appears in `ps`.
printf 'user = "api:%s"\n' "$(cat "$TMP/mgkey.trim")" >"$TMP/curlrc"

mg() { # mg <method> <path> [curl args...]
  local method="$1" path="$2"; shift 2
  curl -sS -K "$TMP/curlrc" -o "$TMP/resp" -w '%{http_code}' -X "$method" "$MG_API$path" "$@"
}

fail=0
for t in "${tenants[@]}"; do
  d="$(domain_for "$t")"
  login="${LOGIN_LOCALPART}@${d}"
  echo
  echo "-- $t ($d)"

  code="$(mg GET "/v4/domains/$d")"
  state="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("domain",{}).get("state",""))' "$TMP/resp" 2>/dev/null || true)"
  if [ "$code" != "200" ] || [ "$state" != "active" ]; then
    echo "   domain not active in Mailgun (http $code, state '${state}') — provision it first (runbook)"; fail=1; continue
  fi
  echo "   domain active"

  code="$(mg GET "/v3/domains/$d/credentials")"
  exists="$(python3 -c 'import json,sys;print(any(i.get("login")==sys.argv[2] for i in json.load(open(sys.argv[1])).get("items",[])))' "$TMP/resp" "$login" 2>/dev/null || echo False)"
  echo "   login $login exists: $exists"

  if [ $APPLY -eq 0 ]; then
    echo "   DRY-RUN: would $([ "$exists" = True ] && echo 'reset password of' || echo 'create') $login, store on 1P, verify, SMTP-AUTH test"
    continue
  fi

  python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))' >"$TMP/pw.$t"
  if [ "$exists" = True ]; then
    code="$(mg PUT "/v3/domains/$d/credentials/$login" --data-urlencode "password@$TMP/pw.$t")"
  else
    code="$(mg POST "/v3/domains/$d/credentials" --data-urlencode "login=$login" --data-urlencode "password@$TMP/pw.$t")"
  fi
  case "$code" in
    200|201) echo "   Mailgun credential $([ "$exists" = True ] && echo reset || echo created) (http $code)" ;;
    *) echo "   Mailgun credential write FAILED (http $code):"; cat "$TMP/resp"; echo; fail=1; continue ;;
  esac

  # Store on 1P. Pass the password via a file read inside the argument expansion so
  # it never hits history; op may print a spurious error even when the write lands.
  if ! op item edit "$ITEM" --vault "$VAULT" \
        "smtp_login_news_${t}[text]=$login" \
        "smtp_password_news_${t}[concealed]=$(cat "$TMP/pw.$t")" >/dev/null 2>"$TMP/op.err"; then
    echo "   op item edit reported an error (often spurious) — verifying by re-read:"; sed 's/^/     /' "$TMP/op.err"
  fi
  op item get "$ITEM" --vault "$VAULT" --fields "label=smtp_login_news_${t}" --reveal 2>/dev/null | tr -d '\r\n' >"$TMP/rl.$t" || true
  op item get "$ITEM" --vault "$VAULT" --fields "label=smtp_password_news_${t}" --reveal 2>/dev/null | tr -d '\r\n' >"$TMP/rp.$t" || true
  if [ "$(cat "$TMP/rl.$t")" = "$login" ] && cmp -s "$TMP/rp.$t" <(tr -d '\r\n' <"$TMP/pw.$t"); then
    echo "   1P fields smtp_login_news_${t} / smtp_password_news_${t} verified by re-read"
  else
    echo "   1P re-read MISMATCH — the stored fields do not equal what was minted. Fix the item by hand, then re-run --only $t."; fail=1; continue
  fi

  # SMTP AUTH test with the STORED values (what Odoo will use).
  if python3 - "$login" "$TMP/rp.$t" "$SMTP_HOST" "$SMTP_PORT" <<'PY'
import smtplib, sys
login, pwfile, host, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
pw = open(pwfile).read().strip()
with smtplib.SMTP(host, port, timeout=30) as s:
    s.starttls()
    s.login(login, pw)
PY
  then echo "   SMTP AUTH ok on $SMTP_HOST:$SMTP_PORT STARTTLS"
  else echo "   SMTP AUTH FAILED for $login (Mailgun can lag a few seconds after a reset — re-run --only $t)"; fail=1
  fi
done

echo
if [ $fail -ne 0 ]; then echo "DONE WITH FAILURES (see above)"; exit 2; fi
if [ $APPLY -eq 0 ]; then echo "DRY-RUN complete. Re-run with --apply."; else echo "ALL FOUR STORED + VERIFIED. Next: the Odoo rows — see docs/RUNBOOK-release-train.md 'Gate: newsletter bulk mail servers'."; fi
