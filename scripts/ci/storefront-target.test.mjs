#!/usr/bin/env node
// Behavioral tests for storefront-target.mjs (promote-storefronts.yml prepare).
// Run: `node scripts/ci/storefront-target.test.mjs`
//
// Replays the 2026-09-30 incident (run 36787550565): main HEAD = a CI-only
// commit with no images; blank target_sha must resolve to the newest BUILT
// commit instead, and an explicit image-less SHA must fail before the gate.
import assert from "node:assert/strict";
import {
  appCodeHits, pickNewestBuilt, resolveTarget, renderSummary, makeGithub, makeGhcr, isFullSha,
} from "./storefront-target.mjs";

const HEAD = "99d4d59f".padEnd(40, "0");  // grove-sites #933, docs/runbooks + scripts/ci
const BUILT = "ba36ff2f4e70b1007106aaf0455606adfa0f306e";
const OLDER = "f62c5118c8cefd9cf3184d7f0f112fcb7899f8c7";

function fakeGithub({ head = HEAD, mainShas = [HEAD, BUILT, OLDER], built = [BUILT, OLDER], files = {} } = {}) {
  const calls = [];
  return {
    calls,
    mainHead: async () => head,
    mainShas: async () => mainShas,
    builtShas: async () => built,
    compare: async (base, h) => {
      calls.push(["compare", base, h]);
      const i = mainShas.indexOf(base);
      const newer = mainShas.slice(0, i).reverse(); // oldest-first, like GitHub
      return {
        status: i === -1 ? "diverged" : "ahead",
        commits: newer.map((sha) => ({ sha, subject: `commit ${sha.slice(0, 8)}` })),
        files: newer.flatMap((s) => files[s] || []),
        filesTruncated: false,
      };
    },
    commitFiles: async (sha) => files[sha] || [],
  };
}
const fakeGhcr = (present) => ({ manifestStatus: async (image, tag) => (present.has(tag) ? 200 : 404) });

// ── pure helpers ────────────────────────────────────────────────────────────
assert.ok(isFullSha(BUILT));
assert.ok(!isFullSha("99d4d59f"));
assert.ok(!isFullSha(BUILT.toUpperCase()));
assert.deepEqual(appCodeHits(["docs/runbooks/x.md", "scripts/ci/y.sh"]), []);
assert.deepEqual(appCodeHits(["apps/nursery/page.tsx", "README.md"]), ["apps/nursery/page.tsx"]);
assert.deepEqual(appCodeHits(["pnpm-lock.yaml", ".github/workflows/docker.yml"]).length, 2);
assert.deepEqual(appCodeHits([".github/workflows/ci.yml"]), [], "only docker.yml itself triggers a build");
assert.equal(pickNewestBuilt([HEAD, BUILT, OLDER], [OLDER, BUILT]), BUILT, "newest by main order, not run order");
assert.equal(pickNewestBuilt([HEAD], [BUILT]), null);

// ── the incident: blank target, CI-only HEAD -> resolves to BUILT ───────────
{
  const gh = fakeGithub({ files: { [HEAD]: ["docs/runbooks/prod-frontend-deploy.md", "scripts/ci/check.sh"] } });
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT, OLDER])) });
  assert.equal(r.ok, true, r.errors.join("; "));
  assert.equal(r.sha, BUILT);
  assert.equal(r.source, "newest-built");
  assert.equal(r.images.length, 4);
  assert.deepEqual(r.skipped.map((c) => c.sha), [HEAD]);
  assert.deepEqual(r.skipped[0].appHits, [], "skipped CI-only commit touches no app code");
  assert.deepEqual(r.warnings, [], "no warning when skipped commits are non-app");
  const md = renderSummary(r);
  assert.match(md, /newest grove-sites `main` commit with a successful `docker.yml` run/);
  assert.match(md, /1 main commit\(s\) skipped/);
  assert.match(md, /\| no \|/);
}

// ── explicit image-less SHA (what the 09-30 run effectively did) -> fail ────
{
  const r = await resolveTarget({ inputSha: HEAD, github: fakeGithub(), ghcr: fakeGhcr(new Set([BUILT])) });
  assert.equal(r.ok, false);
  assert.equal(r.errors.length, 4, "every missing image is named, not just the first");
  assert.match(r.errors[0], /NOT FOUND \(404\)/);
  assert.match(renderSummary(r), /\*\*NO \(404\)\*\*/);
}

// ── explicit good SHA == HEAD -> ok, nothing skipped, no compare call ───────
{
  const gh = fakeGithub({ head: BUILT, mainShas: [BUILT, OLDER] });
  const r = await resolveTarget({ inputSha: BUILT, github: gh, ghcr: fakeGhcr(new Set([BUILT])) });
  assert.equal(r.ok, true);
  assert.deepEqual(r.skipped, []);
  assert.equal(gh.calls.length, 0);
  assert.match(renderSummary(r), /no commits skipped/);
}

// ── skipped commit TOUCHES app code (HEAD build red/in flight) -> warn, not fail
{
  const gh = fakeGithub({ files: { [HEAD]: ["apps/nursery/app/page.tsx"] } });
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT])) });
  assert.equal(r.ok, true);
  assert.equal(r.sha, BUILT);
  assert.equal(r.warnings.length, 1);
  assert.match(r.warnings[0], /TOUCH APP CODE/);
  assert.match(renderSummary(r), /\*\*YES\*\* \(`apps\/nursery\/app\/page.tsx`\)/);
}

// ── partial publish (one matrix leg missing) -> fail naming that image ──────
{
  const ghcr = { manifestStatus: async (image) => (image === "grove-ggg" ? 404 : 200) };
  const r = await resolveTarget({ inputSha: BUILT, github: fakeGithub({ head: BUILT }), ghcr });
  assert.equal(r.ok, false);
  assert.equal(r.errors.length, 1);
  assert.match(r.errors[0], /grove-ggg/);
}

// ── malformed / no built commit ─────────────────────────────────────────────
{
  const r = await resolveTarget({ inputSha: "99d4d59f", github: fakeGithub(), ghcr: fakeGhcr(new Set()) });
  assert.equal(r.ok, false);
  assert.match(r.errors[0], /not a 40-char/);
  const r2 = await resolveTarget({ inputSha: "", github: fakeGithub({ built: [] }), ghcr: fakeGhcr(new Set()) });
  assert.equal(r2.ok, false);
  assert.match(r2.errors[0], /Pass target_sha explicitly/);
}

// ── explicit SHA not on main -> warning ─────────────────────────────────────
{
  const offMain = "a".repeat(40);
  const r = await resolveTarget({ inputSha: offMain, github: fakeGithub(), ghcr: fakeGhcr(new Set([offMain])) });
  assert.equal(r.ok, true);
  assert.match(r.warnings.join(" "), /not an ancestor/);
}

// ── IO adapters: GHCR uses an anonymous pull token (bare requests always 401)
{
  const seen = [];
  const fetchImpl = async (url, opts = {}) => {
    seen.push([url, opts.method || "GET", opts.headers?.Authorization]);
    if (url.startsWith("https://ghcr.io/token")) return { ok: true, status: 200, json: async () => ({ token: "anon" }) };
    return { ok: false, status: url.endsWith(BUILT) ? 200 : 404 };
  };
  const ghcr = makeGhcr({ fetchImpl, retryDelayMs: 0 });
  assert.equal(await ghcr.manifestStatus("grove-hub", BUILT), 200);
  assert.equal(await ghcr.manifestStatus("grove-hub", HEAD), 404);
  assert.match(seen[0][0], /scope=repository:goldberry-playground\/grove-hub:pull/);
  assert.deepEqual(seen[1].slice(1), ["HEAD", "Bearer anon"]);
}

// ── GitHub adapter: 5xx retried, 404 not ────────────────────────────────────
{
  let n = 0;
  const flaky = async () => (++n < 2 ? { ok: false, status: 502 } : { ok: true, status: 200, json: async () => ({ sha: BUILT }) });
  assert.equal(await makeGithub({ fetchImpl: flaky, retryDelayMs: 0 }).mainHead(), BUILT);
  assert.equal(n, 2);
  let m = 0;
  const missing = async () => (++m, { ok: false, status: 404 });
  await assert.rejects(makeGithub({ fetchImpl: missing, retryDelayMs: 0 }).mainHead(), /HTTP 404/);
  assert.equal(m, 1, "4xx is permanent, not retried");
}

console.log("storefront-target: all assertions passed");
