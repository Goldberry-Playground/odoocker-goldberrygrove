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

# Mirror the tree exactly (delete removed files). Keep it lean — the bridge is
# zero-dependency, so there is no node_modules/build output to exclude.
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$SRC/." "$DEST/"

echo "Synced $SRC -> $DEST"
echo "Review: git status $DEST ; then commit the snapshot."
