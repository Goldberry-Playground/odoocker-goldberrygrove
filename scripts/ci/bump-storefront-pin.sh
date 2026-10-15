#!/usr/bin/env bash
# Bump the prod storefront image-tag pins in a variables.tf, in place.
#
#   scripts/ci/bump-storefront-pin.sh <path/to/variables.tf> <40-hex grove-sites sha>
#
# Prints exactly one word on stdout: `changed` or `nochange`. Exits non-zero on
# a bad argument or if the edit touched anything but the two storefront pins.
#
# Called twice by promote-storefronts.yml: once on the runner's working tree
# (the targeted terraform apply reads that), and once inside a throwaway
# worktree of the CURRENT remote main by promote-reconcile-branch.sh (the
# reconcile PR is built there). Both edits must be the same transformation,
# which is why it lives in one script.
#
# The replace is anchored to the `hub_image_tag` / `tenant_image_tag` blocks
# ONLY. A bare `s/default = "40hex"/.../g` would also rewrite
# `custom_modules_ref` -- the grove-odoo-modules git-sync pin, a DIFFERENT
# repo -- silently repointing prod's Odoo modules at a grove-sites SHA that
# git-sync can't resolve (GOL-1708 finding 1). The post-edit check below is the
# belt to that anchor's suspenders.
set -euo pipefail

FILE="${1:?usage: bump-storefront-pin.sh <variables.tf> <40-hex sha>}"
SHA="${2:?usage: bump-storefront-pin.sh <variables.tf> <40-hex sha>}"

if ! [[ "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::target '$SHA' is not a 40-char lowercase hex commit SHA" >&2
  exit 1
fi
if [ ! -f "$FILE" ]; then
  echo "::error::$FILE does not exist" >&2
  exit 1
fi

# The 40-hex `default` inside ONE named variable block of a file (same awk the
# prepare job's summary uses, so "currently pinned" and this guard agree).
pin_default() {
  awk -v v="$2" '
    $0 ~ ("^variable \"" v "\" \\{") { f = 1 }
    f && /default[[:space:]]*=/ { match($0, /[0-9a-f]{40}/); print substr($0, RSTART, RLENGTH); exit }
  ' "$1"
}

WORK="$(mktemp)"
trap 'rm -f "$WORK"' EXIT
cp "$FILE" "$WORK"

# No `sed -i`: its flag differs between GNU and BSD sed and the test runs on
# macOS too. Edit the scratch copy, then compare it to the original.
for v in hub_image_tag tenant_image_tag; do
  sed -E "/^variable \"$v\" \{/,/^\}/ s|^([[:space:]]*default[[:space:]]*=[[:space:]]*\")[0-9a-f]{40}(\")|\1${SHA}\2|" "$WORK" > "$WORK.next"
  mv "$WORK.next" "$WORK"
done

if [ "$(pin_default "$WORK" custom_modules_ref)" != "$(pin_default "$FILE" custom_modules_ref)" ]; then
  echo "::error::pin-bump touched custom_modules_ref -- aborting (GOL-1708 finding 1 guard)." >&2
  exit 1
fi
for v in hub_image_tag tenant_image_tag; do
  if [ "$(pin_default "$WORK" "$v")" != "$SHA" ]; then
    echo "::error::$v is not at $SHA after the edit -- is its \`variable\` block still shaped as expected?" >&2
    exit 1
  fi
done

if cmp -s "$WORK" "$FILE"; then
  echo "nochange"
  exit 0
fi
cat "$WORK" > "$FILE"
echo "changed"
