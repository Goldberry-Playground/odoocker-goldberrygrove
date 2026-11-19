#!/usr/bin/env bash
# hub-assets-env.sh <apps.json> <env-out-file>
#
# Carries the hub's LIVE GROVE_ASSETS_* values through the promote apply
# (promote-storefronts.yml), so a promotion never changes them.
#
# Why: the promote job targets digitalocean_app.hub, whose spec sets
# GROVE_ASSETS_KEY / GROVE_ASSETS_SECRET / GROVE_ASSETS_OPTIMIZE_TOKEN from
# var.grove_assets_* (apps.tf). The job used to pass the literal
# "ci-promote-placeholder" for all three, so every promotion overwrote the
# hub's real Spaces credentials and optimize bearer with that string. Found on
# 2026-11-18: the Train #5 promote left the hub on placeholders from 15:38Z
# until an operator apply restored them at 21:44Z.
#
# The real values live in `op://Goldberry Grove - Admin/Grove Infra/*`, which
# the promote job's 1Password service account (scoped to `Grove Prod`) cannot
# read. Rather than mirror three credentials into a second vault (a copy that
# goes stale on the next rotation), read what the hub is running right now and
# hand the same values back to Terraform. The operator apply stays the only
# place these credentials are set or rotated.
#
# <apps.json> is the DigitalOcean apps list: either the API body
# ({"apps":[...]}, GET /v2/apps) or `doctl apps list -o json` (a bare array).
# For each key this appends `TF_VAR_<name>=<value>` to <env-out-file> and
# prints a `::add-mask::` line so the value is redacted from the job log.
# Values are never printed any other way.
#
# Fails closed (exit 1, nothing written) when the hub app is missing or any of
# the three values is absent, empty, a placeholder, multi-line, or an encrypted
# `EV[...]` blob. A hub that is already on a placeholder must be repaired by an
# operator apply first; promoting must not make that state permanent.

set -euo pipefail

HUB_APP_NAME="${HUB_APP_NAME:-grove-hub-prod}"

if [ "$#" -ne 2 ]; then
  echo "usage: hub-assets-env.sh <apps.json> <env-out-file>" >&2
  exit 2
fi
apps_json="$1"
out_file="$2"

# env key on the hub service -> Terraform variable name
pairs="GROVE_ASSETS_KEY:grove_assets_key
GROVE_ASSETS_SECRET:grove_assets_secret
GROVE_ASSETS_OPTIMIZE_TOKEN:grove_assets_optimize_token"

hub="$(jq -c --arg name "$HUB_APP_NAME" '
  (if type == "array" then . else (.apps // []) end)
  | map(select(.spec.name == $name)) | .[0] // empty
' "$apps_json")"

if [ -z "$hub" ]; then
  echo "::error::hub-assets-env: no app named $HUB_APP_NAME in the apps list" >&2
  exit 1
fi

lines=""
masks=""
bad=0
while IFS=: read -r env_key tf_var; do
  # Service-level envs win over app-level ones, as on App Platform.
  value="$(jq -r --arg k "$env_key" '
    ([.spec.services[]?.envs[]? | select(.key == $k) | .value]
     + [.spec.envs[]? | select(.key == $k) | .value])
    | map(select(. != null)) | .[0] // ""
  ' <<<"$hub")"

  reason=""
  lower="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  if [ -z "$value" ]; then
    reason="is missing or empty"
  elif [[ "$lower" == *placeholder* ]]; then
    reason="is a placeholder"
  elif [[ "$value" == EV\[* ]]; then
    reason="is an encrypted EV[...] value (cannot be carried through)"
  elif [[ "$value" == *$'\n'* ]]; then
    reason="spans more than one line"
  fi

  if [ -n "$reason" ]; then
    echo "::error::hub-assets-env: live $env_key on $HUB_APP_NAME $reason. Restore it with an operator apply (environments/production, -target=digitalocean_app.hub) before promoting." >&2
    bad=1
    continue
  fi

  masks+="::add-mask::${value}"$'\n'
  lines+="TF_VAR_${tf_var}=${value}"$'\n'
done <<<"$pairs"

if [ "$bad" -ne 0 ]; then
  exit 1
fi

printf '%s' "$masks"
printf '%s' "$lines" >>"$out_file"
echo "hub-assets-env: carried 3 live GROVE_ASSETS_* values from $HUB_APP_NAME"
