#!/usr/bin/env bash
# github-push-doctor.sh — diagnose the agent-plane GitHub push path (GOL-2571).
#
# Run this BEFORE filing a "cannot push / broker down" issue. It walks the whole
# chain in the order it actually breaks and tells you which link failed. It is
# strictly read-only: it mints a short-lived token and does `ls-remote`, never a
# push, and it never prints a token body.
#
# Usage:  scripts/agent-git/github-push-doctor.sh [<owner>/<repo>]
#         (defaults to the `origin` remote of the current checkout)
#
# Exit 0 = the push path is healthy and the fault is in how you invoked it;
#          the "COPY-PASTE" block at the end is the invocation that works.
# Exit 1 = a real outage. The escalation block tells you what to hand to Josh.
#
# Background — the two false-alarm classes this exists to kill:
#
#   1. `owner_not_allowed` from the broker (GOL-2571). The broker takes owner and
#      repo as TWO SEPARATE query params. `?repo=<owner>/<repo>` leaves `owner`
#      empty, an empty owner is not in the allowlist, and you get a structured
#      403 `owner_not_allowed` that reads like a server-side allowlist problem.
#      It is not. `?owner=<owner>&repo=<repo>` works. A no-param `/token` also
#      returns `owner_not_allowed` for the same reason — that is NOT evidence of
#      an unset/closed-by-default allowlist.
#
#   2. `gh auth status` reporting the hosts.yml token invalid (GOL-2571).
#      /paperclip/.config/gh/hosts.yml holds a GitHub *App installation* token,
#      which GitHub expires after ONE HOUR. It has been dead since an hour after
#      it was written and always will be — the file is a dead artifact, not a
#      credential that "recently expired". Nothing reads it: `git` goes through
#      the credential helper, and `gh` takes `GH_TOKEN` from the environment,
#      which overrides hosts.yml. Ignore that line.
#
# See docs/RUNBOOK-agent-github-push.md for the full writeup.

set -uo pipefail

BROKER="${GH_TOKEN_BROKER_URL:-http://gh-token-broker:9099}"
HELPER="${AGENT_GIT_HELPER:-/paperclip/agent-git/github-app-token.mjs}"
KEYFILE="${GH_BROKER_API_KEY_FILE:-/paperclip/gh-broker.key}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
info() { printf '  ....  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Redact anything token-shaped, plus JWT-ish trailers, from any output we echo.
redact() { sed -E 's/(gh[a-z]_)[A-Za-z0-9_.\-]+/\1<REDACTED>/g; s/[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}/<REDACTED-JWT>/g'; }

SLUG="${1:-}"
if [[ -z "$SLUG" ]]; then
  url="$(git remote get-url origin 2>/dev/null || true)"
  if [[ -z "$url" ]]; then
    echo "No <owner>/<repo> given and no 'origin' remote here. Pass it explicitly." >&2
    exit 2
  fi
  # Handles https://github.com/o/r(.git) and git@github.com:o/r(.git)
  SLUG="$(printf '%s' "$url" | sed -E 's#^.*github\.com[/:]##; s#\.git$##')"
fi
OWNER="${SLUG%%/*}"
REPO="${SLUG##*/}"
if [[ -z "$OWNER" || -z "$REPO" || "$OWNER" == "$SLUG" ]]; then
  echo "Could not split '$SLUG' into <owner>/<repo>." >&2
  exit 2
fi

echo "github-push-doctor — owner=$OWNER repo=$REPO broker=$BROKER"
FAILED=""

head_ "1. Broker reachable"
# The broker is a SEPARATE CONTAINER. localhost:9099 is always refused and has
# caused repeated false "broker down" escalations (GOL-2404, GOL-2273, GOL-1545).
hcode="$(curl -s -m 15 -o "$TMP/health" -w '%{http_code}' "$BROKER/health" 2>/dev/null)"
hcode="${hcode:-000}"
if [[ "$hcode" == "200" ]]; then
  pass "$BROKER/health -> 200 $(redact < "$TMP/health" 2>/dev/null)"
else
  fail "$BROKER/health -> HTTP $hcode (expected 200)"
  info "If you probed localhost:9099, that is NOT the broker and is always refused."
  FAILED="broker-health"
fi

head_ "2. Broker API key present"
if [[ -r "$KEYFILE" ]]; then
  klen="$(wc -c < "$KEYFILE" | tr -d ' ')"
  pass "$KEYFILE readable (${klen} bytes)"
  KEY="$(cat "$KEYFILE")"
else
  fail "$KEYFILE missing or unreadable"
  FAILED="${FAILED:+$FAILED,}broker-key"
  KEY=""
fi

head_ "3. Broker mint — CORRECT param shape (owner= and repo= separate)"
if [[ -n "$KEY" && -z "$FAILED" ]]; then
  body="$(curl -s -m 30 -w $'\n%{http_code}' -H "Authorization: Bearer $KEY" \
            "$BROKER/token?owner=$OWNER&repo=$REPO" 2>/dev/null)"
  code="${body##*$'\n'}"
  if [[ "$code" == "200" ]] && printf '%s' "$body" | grep -q '"token"'; then
    exp="$(printf '%s' "$body" | sed -nE 's/.*"expires_at":"([^"]*)".*/\1/p')"
    pass "GET /token?owner=&repo= -> 200, token minted${exp:+ (expires $exp)}"
  else
    fail "GET /token?owner=$OWNER&repo=$REPO -> HTTP $code"
    info "$(printf '%s' "${body%$'\n'*}" | redact | head -c 300)"
    if printf '%s' "$body" | grep -q owner_not_allowed; then
      info "owner_not_allowed WITH a correct owner= param IS a real allowlist gap -> escalate."
    fi
    FAILED="${FAILED:+$FAILED,}broker-mint"
  fi
else
  info "skipped (earlier check failed)"
fi

head_ "4. Broker mint — WRONG param shape (the GOL-2571 trap, shown on purpose)"
if [[ -n "$KEY" && "$hcode" == "200" ]]; then
  wcode="$(curl -s -m 30 -o "$TMP/wrong" -w '%{http_code}' -H "Authorization: Bearer $KEY" \
             "$BROKER/token?repo=$OWNER/$REPO" 2>/dev/null)"
  wcode="${wcode:-000}"
  info "GET /token?repo=$OWNER/$REPO -> HTTP $wcode $(redact < "$TMP/wrong" 2>/dev/null)"
  if [[ "$wcode" == "403" ]]; then
    info "EXPECTED. Full slug in repo= leaves owner= empty -> owner_not_allowed."
    info "This 403 is a CLIENT bug, not a broker outage. Use the shape in step 3."
  fi
else
  info "skipped (broker unreachable or no key — nothing to demonstrate)"
fi

head_ "5. git credential helper (what \`git push\` actually uses)"
if [[ -r "$HELPER" ]]; then
  if tok="$(node "$HELPER" token "$OWNER/$REPO" 2>"$TMP/helper-err")"; then
    if [[ "${tok:0:4}" == "ghs_" ]]; then
      pass "node $HELPER token $OWNER/$REPO -> ghs_ token (${#tok} chars)"
    else
      fail "helper returned something that is not a ghs_ token"
      FAILED="${FAILED:+$FAILED,}helper-shape"
    fi
  else
    fail "helper errored: $(redact < "$TMP/helper-err" | head -c 300)"
    FAILED="${FAILED:+$FAILED,}helper"
  fi
else
  fail "$HELPER not found — git pushes cannot authenticate"
  FAILED="${FAILED:+$FAILED,}helper-missing"
fi

head_ "6. Authenticated remote access (read-only proof, no push)"
if git ls-remote --heads "https://github.com/$OWNER/$REPO.git" HEAD >/dev/null 2>"$TMP/lsremote"; then
  pass "git ls-remote via the credential helper succeeded -> push auth is wired"
else
  fail "git ls-remote failed: $(redact < "$TMP/lsremote" | head -c 300)"
  FAILED="${FAILED:+$FAILED,}ls-remote"
fi

head_ "7. gh CLI"
if command -v gh >/dev/null 2>&1; then
  info "gh present. Do NOT trust \`gh auth status\`: /paperclip/.config/gh/hosts.yml"
  info "holds a long-dead 1-hour installation token and always reports invalid."
  info "Correct usage is to pass a freshly minted token via the environment:"
  info "  GH_TOKEN=\"\$(node $HELPER token $OWNER/$REPO)\" gh <cmd>   # env beats hosts.yml"
else
  info "gh not installed — use the REST API with a minted token instead."
fi

if [[ -z "$FAILED" ]]; then
  cat <<EOF

$(printf '\033[1;32m=== PUSH PATH IS HEALTHY ===\033[0m')
The chain is intact, so a push failure you just saw is in the invocation.
Do NOT file a broker/credential outage issue. COPY-PASTE these:

  # push a branch (this is the designed path — the helper handles auth)
  git push origin HEAD:<branch-name>

  # open a PR / call the REST API
  export GH_TOKEN="\$(node $HELPER token $OWNER/$REPO)"
  gh pr create --base main --head <branch-name> --title '...' --body '...'

  # raw broker mint, if you need the token yourself (owner= and repo= SEPARATE)
  curl -s -H "Authorization: Bearer \$(cat $KEYFILE)" \\
    "$BROKER/token?owner=$OWNER&repo=$REPO"

If \`git push\` still fails after this passed, quote its VERBATIM stderr in the
issue — a branch-protection or non-fast-forward rejection is not an outage.
EOF
  exit 0
fi

cat <<EOF

$(printf '\033[1;31m=== REAL FAULT: %s ===\033[0m' "$FAILED")
Escalate to Josh with the block above pasted verbatim. Include:
  - which numbered step failed (the FAIL lines), and
  - the output of: curl -s -m 15 -w ' HTTP=%{http_code}' "$BROKER/health"
Work is not lost meanwhile: commit locally and note the branch + SHA on the issue.
Review-only work does NOT need the broker — PR refs fetch unauthenticated:
  git -c credential.helper= fetch origin refs/pull/<N>/head
EOF
exit 1
