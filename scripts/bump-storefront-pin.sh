#!/usr/bin/env bash
# Bump the prod storefront image-tag pin in a variables.tf, in place.
#
#   scripts/bump-storefront-pin.sh <variables.tf> <40-char grove-sites sha>
#
# Rewrites the `default = "<40hex>"` line inside the hub_image_tag and
# tenant_image_tag variable blocks ONLY. The replace is anchored to those two
# blocks because a bare `s/default = "40hex"/.../g` also rewrites
# custom_modules_ref -- the grove-odoo-modules git-sync pin, a DIFFERENT repo
# -- silently repointing prod's Odoo modules at a grove-sites SHA that git-sync
# cannot resolve (GOL-1708 finding 1). A belt-and-suspenders diff check aborts
# if a changed line mentions custom_modules_ref anyway.
#
# Single source of truth for promote-storefronts.yml, which applies this edit
# TWICE per run: once to the dispatch-time checkout (so the targeted terraform
# apply reads the new pin) and once more to a FRESH origin/main worktree for
# the reconcile PR (GOL-3409 -- the two checkouts can be hours apart under the
# production Environment gate, so they must not share a branch).
#
# Exit codes:  0 changed   3 already at target (no-op)   1 error
set -euo pipefail

FILE="${1:?usage: bump-storefront-pin.sh <variables.tf> <40-char sha>}"
TARGET_SHA="${2:?usage: bump-storefront-pin.sh <variables.tf> <40-char sha>}"

if ! [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::bump-storefront-pin: target '$TARGET_SHA' is not a 40-char lowercase hex SHA" >&2
  exit 1
fi
[ -f "$FILE" ] || { echo "::error::bump-storefront-pin: no such file: $FILE" >&2; exit 1; }

BEFORE="$(mktemp)"
trap 'rm -f "$BEFORE"' EXIT
cp "$FILE" "$BEFORE"

for v in hub_image_tag tenant_image_tag; do
  sed -i -E "/^variable \"$v\" \{/,/^\}/ s|^([[:space:]]*default[[:space:]]*=[[:space:]]*\")[0-9a-f]{40}(\")|\1${TARGET_SHA}\2|" "$FILE"
done

if cmp -s "$BEFORE" "$FILE"; then
  echo "bump-storefront-pin: $FILE already pins $TARGET_SHA -- nothing to change"
  exit 3
fi

# The anchored sed must never touch custom_modules_ref. Abort loudly (and
# restore the file) if any changed line mentions it.
if diff "$BEFORE" "$FILE" | grep -E '^[<>]' | grep -q 'custom_modules_ref'; then
  cp "$BEFORE" "$FILE"
  echo "::error::bump-storefront-pin touched custom_modules_ref -- reverted, aborting (GOL-1708 finding 1 guard)." >&2
  exit 1
fi

echo "bump-storefront-pin: $FILE hub_image_tag + tenant_image_tag -> $TARGET_SHA"
