#!/usr/bin/env node
// Behavioral tests for storefront-target.mjs (promote-storefronts.yml prepare).
// Run: `node scripts/ci/storefront-target.test.mjs`
//
// Replays the 2026-09-30 incident (run 36787550565): main HEAD = a CI-only
// commit with no images; blank target_sha must resolve to the newest BUILT
// commit instead, and an explicit image-less SHA must fail before the gate.
import assert from "node:assert/strict";
import {
  appCodeHits, pickNewestBuilt, mdCell, resolveTarget, renderSummary, makeGithub, makeGhcr, isFullSha,
  parsePinnedShas, PIN_VARIABLES,
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
assert.equal(mdCell("a | b"), "a \\| b");
assert.equal(mdCell("trailing \\|x"), "trailing \\\\\\|x", "backslash escaped before pipe (CodeQL #575)");
assert.equal(mdCell("one\ntwo"), "one two");

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

// ── the 2026-10-02 freeze-day flake: a torn view of main ────────────────────
// Two identical dry runs 60s apart resolved differently; the first picked a
// 2026-09-08 commit 50 behind main and 48 behind the live prod pin, reported
// all four images present and exited 0. Reproduced here as what it was: a
// commit listing that disagrees with commits/main about HEAD.
{
  const STALE = [OLDER];                       // stale page: ends well before HEAD
  const gh = { ...fakeGithub(), mainShas: async () => STALE };
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([OLDER])) });
  assert.equal(r.ok, false, "a torn view must not resolve");
  assert.equal(r.sha, null);
  assert.match(r.errors[0], /inconsistent view of grove-sites main twice/);
  assert.match(r.errors[0], /re-run/);
  assert.match(renderSummary(r), /\[!CAUTION\]/);
}

// ...and the same torn read is retried once, because a commit landing between
// the two reads is indistinguishable from a stale replica.
{
  let n = 0;
  const gh = {
    ...fakeGithub(),
    mainShas: async () => (++n === 1 ? [OLDER] : [HEAD, BUILT, OLDER]),
  };
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT])) });
  assert.equal(n, 2, "re-read once before giving up");
  assert.equal(r.ok, true, r.errors.join("; "));
  assert.equal(r.sha, BUILT);
}

// ── rollback guard: auto-resolved target behind the live prod pin -> FATAL ──
{
  const PIN = "d4d248ef04d94f2a64d16010eff6146283e0968a";
  const gh = {
    ...fakeGithub({ head: BUILT, mainShas: [BUILT, OLDER], built: [BUILT, OLDER] }),
    compare: async (base, head) =>
      base === PIN
        ? { status: "behind", aheadBy: 0, behindBy: 48, commits: [], files: [], filesTruncated: false }
        : { status: "ahead", aheadBy: 1, behindBy: 0, commits: [], files: [], filesTruncated: false },
  };
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT])), pinnedShas: [PIN] });
  assert.equal(r.ok, false, "blank target_sha must never resolve to a rollback");
  assert.match(r.errors[0], /48 commit\(s\) BEHIND the live production pin d4d248ef/);
  assert.match(r.errors[0], /stale GitHub read/);
  assert.equal(r.warnings.length, 0);
  assert.match(renderSummary(r), /live production pin: `d4d248ef04d94f2a64d16010eff6146283e0968a`/);
}

// ── ...but an EXPLICIT older SHA is the documented rollback -> warn, proceed ─
{
  const PIN = "d4d248ef04d94f2a64d16010eff6146283e0968a";
  const gh = {
    ...fakeGithub({ head: HEAD }),
    compare: async () => ({ status: "behind", aheadBy: 0, behindBy: 2, commits: [], files: [], filesTruncated: false }),
  };
  const r = await resolveTarget({ inputSha: BUILT, github: gh, ghcr: fakeGhcr(new Set([BUILT])), pinnedShas: [PIN] });
  assert.equal(r.ok, true, "a human rolling back on purpose is not blocked");
  assert.match(r.warnings.join(" "), /deliberate rollback/);
  assert.equal(r.errors.length, 0);
}

// ── diverged from the pin is also a refusal on the auto path ────────────────
{
  const PIN = "d4d248ef04d94f2a64d16010eff6146283e0968a";
  const gh = {
    ...fakeGithub({ head: BUILT, mainShas: [BUILT, OLDER] }),
    compare: async () => ({ status: "diverged", aheadBy: 3, behindBy: 4, commits: [], files: [], filesTruncated: false }),
  };
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT])), pinnedShas: [PIN] });
  assert.equal(r.ok, false);
  assert.match(r.errors[0], /NOT on the lineage of \(diverged from\) the live production pin/);
}

// ── target ahead of the pin (the normal promote) -> no rollback finding ─────
{
  const PIN = "d4d248ef04d94f2a64d16010eff6146283e0968a";
  const gh = {
    ...fakeGithub({ head: BUILT, mainShas: [BUILT, OLDER] }),
    compare: async () => ({ status: "ahead", aheadBy: 2, behindBy: 0, commits: [], files: [], filesTruncated: false }),
  };
  const r = await resolveTarget({ inputSha: "", github: gh, ghcr: fakeGhcr(new Set([BUILT])), pinnedShas: [PIN] });
  assert.equal(r.ok, true, r.errors.join("; "));
  assert.equal(r.warnings.length, 0);
}

// ── pin equal to the target short-circuits (no compare, no finding) ────────
{
  const gh = fakeGithub({ head: BUILT, mainShas: [BUILT, OLDER] });
  const r = await resolveTarget({ inputSha: BUILT, github: gh, ghcr: fakeGhcr(new Set([BUILT])), pinnedShas: [BUILT] });
  assert.equal(r.ok, true);
  assert.equal(gh.calls.length, 0, "re-promoting the live pin asks GitHub nothing extra");
}

// ── reading the live pins out of the prod variables.tf ──────────────────────
{
  const tf = [
    'variable "hub_image_tag" {',
    "  description = \"grove-sites commit SHA\"",
    "  type        = string",
    '  default     = "d4d248ef04d94f2a64d16010eff6146283e0968a"',
    "}",
    "",
    'variable "tenant_image_tag" {',
    '  default     = "d4d248ef04d94f2a64d16010eff6146283e0968a"',
    "}",
  ].join("\n");
  assert.deepEqual(parsePinnedShas(tf), ["d4d248ef04d94f2a64d16010eff6146283e0968a"], "duplicates collapse");

  const split = tf.replace(/tenant_image_tag" \{\n  default     = "[0-9a-f]+"/,
    'tenant_image_tag" {\n  default     = "' + "a".repeat(40) + '"');
  assert.equal(parsePinnedShas(split).length, 2, "a half-rolled prod yields BOTH pins");

  // The `default` must come from inside the named block, like the workflow's awk.
  const noDefault = [
    'variable "hub_image_tag" {',
    "  type        = string",
    "}",
    "",
    'variable "something_else" {',
    '  default     = "d4d248ef04d94f2a64d16010eff6146283e0968a"',
    "}",
  ].join("\n");
  assert.throws(() => parsePinnedShas(noDefault, ["hub_image_tag"]), /no 40-hex `default` SHA/,
    "a neighbour's default must not be mistaken for the pin");
  assert.throws(() => parsePinnedShas("", ["hub_image_tag"]), /no `variable "hub_image_tag"` block/);
  assert.deepEqual(PIN_VARIABLES, ["hub_image_tag", "tenant_image_tag"]);
}

console.log("storefront-target: all assertions passed");
