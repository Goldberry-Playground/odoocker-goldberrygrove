#!/usr/bin/env node
// Behavioral tests for consult-target.mjs (promote-storefronts.yml prepare,
// the optional `consult_tag` input -- GOL-3723).
// Run: `node scripts/ci/consult-target.test.mjs`
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { verifyConsultTarget, renderConsultSummary, makeConsultGithub, CONSULT_PIN_VARIABLE } from "./consult-target.mjs";
import { parsePinnedShas, PROD_TF_VARIABLES } from "./storefront-target.mjs";

// AgriforestryOS main, newest first.
const NEWER = "fc67652a0d3e5b1c9f8e7d6c5b4a39281706f5e4";
const PIN = "01c34e2134f848fdfc3073c76b56f8cb142fb7a7";
const OLDER = "a58980c0f1e2d3c4b5a69788970a1b2c3d4e5f60";
const MAIN = [NEWER, PIN, OLDER];
const BRANCH_ONLY = "a1765b25e49ff731931f7f8096510e5e35669b57"; // a PR head, never on main
// A grove-sites SHA: unknown to AgriforestryOS, but `grove-consult` still has
// a tag for it from grove-sites' retired consult leg.
const SITES_SHA = "9301254bdc011d32304596781561b4c654552881";

const fakeGithub = () => {
  const calls = [];
  const known = new Set([...MAIN, BRANCH_ONLY]);
  return {
    calls,
    compare: async (base, head) => {
      calls.push([base, head]);
      const b = base === "main" ? MAIN[0] : base;
      const h = head === "main" ? MAIN[0] : head;
      if (!known.has(b) || !known.has(h)) return null;
      const bi = MAIN.indexOf(b);
      const hi = MAIN.indexOf(h);
      if (bi === -1 || hi === -1) return { status: "diverged", aheadBy: 1, behindBy: 1 };
      if (bi === hi) return { status: "identical", aheadBy: 0, behindBy: 0 };
      // lower index = newer
      return hi < bi ? { status: "ahead", aheadBy: bi - hi, behindBy: 0 } : { status: "behind", aheadBy: 0, behindBy: hi - bi };
    },
  };
};
const fakeGhcr = (present) => ({
  calls: [],
  manifestStatus: async function (image, tag) {
    this.calls.push([image, tag]);
    return present.has(tag) ? 200 : 404;
  },
});

// Blank input: nothing is looked up, nothing moves.
{
  const github = fakeGithub();
  const ghcr = fakeGhcr(new Set());
  for (const blank of ["", "   ", undefined]) {
    const r = await verifyConsultTarget({ inputTag: blank, github, ghcr, pinnedSha: PIN });
    assert.equal(r.ok, true);
    assert.equal(r.sha, "");
    assert.equal(r.skipped, true);
  }
  assert.equal(github.calls.length, 0, "blank must not hit GitHub");
  assert.equal(ghcr.calls.length, 0, "blank must not hit GHCR");
  assert.match(renderConsultSummary({ ok: true, sha: "", skipped: true, pinnedSha: PIN, errors: [], warnings: [] }), /not moved/);
}

// Happy path: a newer main commit with an image.
{
  const ghcr = fakeGhcr(new Set([NEWER]));
  const r = await verifyConsultTarget({ inputTag: ` ${NEWER}\n`, github: fakeGithub(), ghcr, pinnedSha: PIN });
  assert.equal(r.ok, true, r.errors.join("; "));
  assert.equal(r.sha, NEWER);
  assert.deepEqual(r.warnings, []);
  assert.deepEqual(ghcr.calls, [["grove-consult", NEWER]], "checks the grove-consult image at the full SHA");
  const md = renderConsultSummary(r);
  assert.match(md, new RegExp(`compare/${PIN}\\.\\.\\.${NEWER}`));
}

// Re-pinning the current pin is fine (re-roll) and makes no rollback claim.
{
  const r = await verifyConsultTarget({ inputTag: PIN, github: fakeGithub(), ghcr: fakeGhcr(new Set([PIN])), pinnedSha: PIN });
  assert.equal(r.ok, true);
  assert.deepEqual(r.warnings, []);
}

// Not a SHA: rejected before any lookup.
for (const bad of ["latest", "main", NEWER.slice(0, 12), NEWER.toUpperCase()]) {
  const github = fakeGithub();
  const r = await verifyConsultTarget({ inputTag: bad, github, ghcr: fakeGhcr(new Set([bad])), pinnedSha: PIN });
  assert.equal(r.ok, false, `${bad} must be rejected`);
  assert.equal(github.calls.length, 0);
}

// A grove-sites SHA in the consult box: the image EXISTS (old leg's tag), and
// it must still be refused because it is not an AgriforestryOS commit.
{
  const r = await verifyConsultTarget({ inputTag: SITES_SHA, github: fakeGithub(), ghcr: fakeGhcr(new Set([SITES_SHA])), pinnedSha: PIN });
  assert.equal(r.ok, false);
  assert.match(r.errors.join("\n"), /not a commit in Goldberry-Playground\/AgriforestryOS/);
}

// A PR-head SHA that never reached main is refused even if someone pushed an image.
{
  const r = await verifyConsultTarget({ inputTag: BRANCH_ONLY, github: fakeGithub(), ghcr: fakeGhcr(new Set([BRANCH_ONLY])), pinnedSha: PIN });
  assert.equal(r.ok, false);
  assert.match(r.errors.join("\n"), /not on .* main/);
}

// On main but never built (path-filtered commit): image gate fails.
{
  const r = await verifyConsultTarget({ inputTag: NEWER, github: fakeGithub(), ghcr: fakeGhcr(new Set()), pinnedSha: PIN });
  assert.equal(r.ok, false);
  assert.match(r.errors.join("\n"), /NOT FOUND \(404\)/);
  assert.match(renderConsultSummary(r), /\*\*NO \(404\)\*\*/);
}

// Explicit rollback: allowed, with a loud warning.
{
  const r = await verifyConsultTarget({ inputTag: OLDER, github: fakeGithub(), ghcr: fakeGhcr(new Set([OLDER])), pinnedSha: PIN });
  assert.equal(r.ok, true);
  assert.equal(r.warnings.length, 1);
  assert.match(r.warnings[0], /1 commit\(s\) BEHIND .* BACKWARD/);
}

// makeConsultGithub: 404 -> null (unknown commit), other failures throw.
{
  const mk = (status, body = {}) => makeConsultGithub({ fetchImpl: async () => ({ ok: status < 400, status, json: async () => body }) });
  assert.equal(await mk(404).compare("a", "b"), null);
  assert.deepEqual(await mk(200, { status: "ahead", ahead_by: 2, behind_by: 0 }).compare("a", "b"), { status: "ahead", aheadBy: 2, behindBy: 0 });
  await assert.rejects(() => mk(500).compare("a", "b"), /HTTP 500/);
}

// The real prod variables.tf carries a parseable consult pin -- the CLI reads
// it for the summary and the rollback warning, and the bump script anchors on
// the same block.
{
  const [pin] = parsePinnedShas(readFileSync(PROD_TF_VARIABLES, "utf8"), [CONSULT_PIN_VARIABLE]);
  assert.match(pin, /^[0-9a-f]{40}$/);
}

console.log("consult-target: all assertions passed");
