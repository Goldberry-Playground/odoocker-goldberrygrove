#!/usr/bin/env bash
# Build + push the storefront reconcile branch ON TOP OF THE CURRENT REMOTE
# MAIN, not on the commit the promote run happened to check out.
#
#   scripts/ci/promote-reconcile-branch.sh <branch> <40-hex grove-sites sha>
#   env: TF_DIR (default infra/terraform/environments/production)
#        REMOTE (default origin)   BASE_BRANCH (default main)
#
# Prints one `result=...` line on stdout:
#   result=pushed base=<main sha> tip=<reconcile sha>   branch pushed, open the PR
#   result=nochange base=<main sha>                      main already carries the pin
#
# WHY A WORKTREE OF origin/main (2026-10-15, run 38621755196): the promote job
# checks out `github.sha` -- main as of DISPATCH -- and the `production`
# Environment gate can park the run for hours (that one waited 16h). Main moved
# meanwhile: #893 changed .github/workflows/prod-modules-pin-drift.yml eight
# minutes after dispatch. The reconcile branch was cut from the stale dispatch
# SHA, so relative to main it still carried the OLD copy of that workflow file,
# and GitHub refused the push ("refusing to allow a GitHub App to create or
# update workflow ... without `workflows` permission") even though the commit
# itself touched only variables.tf. The roll had already succeeded, so main sat
# behind live until a human opened the PR by hand.
#
# Rebuilding the two-line pin edit on main's CURRENT tip means the pushed
# branch differs from main by exactly that edit, so the workflows-permission
# check has nothing to object to and GITHUB_TOKEN keeps its narrow scope (no
# `workflows: write`, no PAT). The runner's own checkout is left untouched: it
# is still what the targeted terraform apply read.
set -euo pipefail

BRANCH="${1:?usage: promote-reconcile-branch.sh <branch> <40-hex sha>}"
SHA="${2:?usage: promote-reconcile-branch.sh <branch> <40-hex sha>}"
REMOTE="${REMOTE:-origin}"
BASE_BRANCH="${BASE_BRANCH:-main}"
TF_DIR="${TF_DIR:-infra/terraform/environments/production}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! [[ "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::target '$SHA' is not a 40-char lowercase hex commit SHA" >&2
  exit 1
fi

git fetch --quiet --no-tags "$REMOTE" "$BASE_BRANCH"

WT="$(mktemp -d)/main"
cleanup() {
  git worktree remove --force "$WT" 2>/dev/null || true
  rm -rf "$(dirname "$WT")"
}
trap cleanup EXIT

git worktree add --quiet --detach "$WT" "$REMOTE/$BASE_BRANCH"
BASE_SHA="$(git -C "$WT" rev-parse HEAD)"

RESULT="$("$HERE/bump-storefront-pin.sh" "$WT/$TF_DIR/variables.tf" "$SHA")"
if [ "$RESULT" = "nochange" ]; then
  echo "result=nochange base=$BASE_SHA"
  exit 0
fi

git -C "$WT" -c user.name="grove-promote-bot" -c user.email="actions@github.com" \
  commit --quiet -am "infra(prod): promote storefronts to ${SHA} (promote-storefronts.yml)"
TIP="$(git -C "$WT" rev-parse HEAD)"
git -C "$WT" push --quiet "$REMOTE" "HEAD:refs/heads/$BRANCH"
echo "result=pushed base=$BASE_SHA tip=$TIP"
