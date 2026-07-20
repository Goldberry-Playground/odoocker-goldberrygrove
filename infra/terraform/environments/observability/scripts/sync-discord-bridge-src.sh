#!/usr/bin/env bash
# Sync the vendored Discord bridge source (GOL-598) from grove-sites.
#
# compose/discord-bridge-src/ is a snapshot of grove-sites apps/discord-bridge,
# vendored here so the obs env can rebuild the interactions endpoint from code
# alone (no cross-repo checkout / secret-bearing clone at provision time). Run
# this whenever the bridge changes upstream, then commit the diff.
#
# Usage: ./scripts/sync-discord-bridge-src.sh /path/to/grove-sites
set -euo pipefail

GROVE_SITES="${1:?usage: sync-discord-bridge-src.sh /path/to/grove-sites}"
SRC="${GROVE_SITES%/}/apps/discord-bridge"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${HERE}/compose/discord-bridge-src"

[ -d "$SRC" ] || { echo "not found: $SRC" >&2; exit 1; }

# Mirror the tree exactly (delete removed files).
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$SRC/." "$DEST/"

# Scrub anything that must never be vendored. The bridge has no RUNTIME deps,
# but it does declare devDependencies (typescript/@types/node for `npm run
# type-check`), so an upstream checkout can absolutely have a node_modules --
# which would otherwise land in the cloud-init b64 blob. A real .env would be
# worse: .gitignore hides it, so it would ride into user_data unnoticed.
# .env.example is intentionally KEPT (it is part of the vendored snapshot).
rm -rf "$DEST/node_modules"
rm -f "$DEST/.env" "$DEST/.env.local" "$DEST/.env.production"

# Tests are not runtime and must not ride into user_data (upstream currently
# carries 8 lib/*.test.ts, ~19 KB). Dropping them here is also what keeps a
# re-sync reproducing the existing lean snapshot instead of showing phantom
# additions in `git status`.
find "$DEST" -name '*.test.ts' -delete

echo "Synced $SRC -> $DEST"
echo "Review: git status $DEST ; then commit the snapshot."
