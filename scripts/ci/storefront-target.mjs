#!/usr/bin/env node
// storefront-target.mjs — resolve + verify the grove-sites build that
// promote-storefronts.yml is about to roll to production.
//
// Why this exists (2026-09-30, run 36787550565): with target_sha blank the
// promote workflow resolved grove-sites `main` HEAD, which was 99d4d59f
// (grove-sites #933, docs/runbooks + scripts/ci only). grove-sites'
// "Docker — Frontends" workflow (docker.yml) is path-filtered to apps/**,
// packages/**, lockfiles etc., so NO images were ever built for that SHA. The
// human approved the production gate against a SHA that had no images, and
// terraform failed on all four DO apps with `404 Image tag or digest not
// found`. Recovery was a re-dispatch pinned to ba36ff2f (newest main commit
// with a green Docker run).
//
// What this does, all BEFORE the approval gate:
//   1. target_sha given  -> use it.
//      target_sha blank  -> newest grove-sites main commit whose docker.yml run
//                           SUCCEEDED (not main HEAD).
//   2. ALWAYS verify ghcr.io/goldberry-playground/grove-{hub,nursery,goldberry,ggg}
//      :<full sha> exist; fail fast naming every missing tag.
//   3. List the main commits being skipped (target..main HEAD) and flag any
//      that touch app code (docker.yml's path filters). App-code skips are a
//      loud WARNING, not a failure: the resolved build is still a coherent,
//      fully-built set of four images, and the reviewer sees the warning in
//      the step summary before approving. (Typical cause: main HEAD's Docker
//      run is still in flight or red.)
//
// CLI (node builtins only; Node >= 18 for global fetch):
//   env INPUT_SHA          optional 40-hex SHA (blank = auto-resolve)
//   env GH_TOKEN           token for api.github.com (grove-sites is public, so
//                          this repo's GITHUB_TOKEN suffices; unset works too,
//                          at the 60 req/h anonymous rate limit)
//   env GITHUB_OUTPUT      if set, `target_sha=<sha>` is appended
//   env GITHUB_STEP_SUMMARY if set, a markdown resolution report is appended
//   exit 0 -> resolved + all four images present
//   exit 1 -> could not resolve, bad SHA, or at least one image missing
//
// Dry run from a laptop (read-only; touches nothing):
//   GH_TOKEN="$(gh auth token)" node scripts/ci/storefront-target.mjs
//   INPUT_SHA=99d4d59f... node scripts/ci/storefront-target.mjs   # -> exit 1
import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { globToRe } from "./protected-paths-carveout.mjs";

export const SITES_REPO = "Goldberry-Playground/grove-sites";
export const DOCKER_WORKFLOW = "docker.yml";
export const GHCR_OWNER = "goldberry-playground";
export const STOREFRONT_IMAGES = ["grove-hub", "grove-nursery", "grove-goldberry", "grove-ggg"];

// Mirror of grove-sites .github/workflows/docker.yml `on.push.paths`. A commit
// touching none of these never triggers an image build. Keep in sync by hand;
// drift here only affects the skipped-commit WARNING, never the image gate.
export const DOCKER_PATH_FILTERS = [
  "apps/**",
  "packages/**",
  "pnpm-lock.yaml",
  "pnpm-workspace.yaml",
  "package.json",
  ".dockerignore",
  ".github/workflows/docker.yml",
];

const SHA_RE = /^[0-9a-f]{40}$/;
export const isFullSha = (s) => SHA_RE.test(s || "");

// Paths (of `files`) that would trigger a docker.yml build.
export function appCodeHits(files, filters = DOCKER_PATH_FILTERS) {
  const matchers = filters.map(globToRe);
  return files.filter((f) => matchers.some((re) => re.test(f)));
}

// First commit in `mainShasNewestFirst` that has a successful build.
export function pickNewestBuilt(mainShasNewestFirst, builtShas) {
  const built = new Set(builtShas);
  return mainShasNewestFirst.find((s) => built.has(s)) || null;
}

// ── IO (injectable for tests) ───────────────────────────────────────────────

async function withRetry(fn, tries = 3, delayMs = 2000) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (i >= tries || e.permanent) throw e;
      await new Promise((r) => setTimeout(r, delayMs * i));
    }
  }
}

export function makeGithub({ fetchImpl = fetch, token = "", retryDelayMs = 2000 } = {}) {
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const get = (path) =>
    withRetry(async () => {
      const res = await fetchImpl(`https://api.github.com/${path}`, { headers });
      if (!res.ok) {
        const err = new Error(`GET ${path} -> HTTP ${res.status}`);
        err.permanent = res.status < 500 && res.status !== 429;
        throw err;
      }
      return res.json();
    }, 3, retryDelayMs);
  return {
    mainHead: async () => (await get(`repos/${SITES_REPO}/commits/main`)).sha,
    mainShas: async () =>
      (await get(`repos/${SITES_REPO}/commits?sha=main&per_page=100`)).map((c) => c.sha),
    builtShas: async () =>
      (
        await get(
          `repos/${SITES_REPO}/actions/workflows/${DOCKER_WORKFLOW}/runs?branch=main&status=success&per_page=100`
        )
      ).workflow_runs.map((r) => r.head_sha),
    // commits: oldest-first (GitHub order); files: union across the range.
    compare: async (base, head) => {
      const c = await get(`repos/${SITES_REPO}/compare/${base}...${head}`);
      return {
        status: c.status,
        commits: c.commits.map((x) => ({ sha: x.sha, subject: (x.commit.message || "").split("\n")[0] })),
        files: (c.files || []).map((f) => f.filename),
        filesTruncated: (c.files || []).length >= 300,
      };
    },
    commitFiles: async (sha) => ((await get(`repos/${SITES_REPO}/commits/${sha}`)).files || []).map((f) => f.filename),
  };
}

// HTTP status of the tag's manifest via an anonymous GHCR pull token. Plain
// tokenless requests always 401 on GHCR, so they can't tell present from
// missing (scripts/check-ghcr-images.sh's 401-is-ok rule) -- don't reuse that.
export function makeGhcr({ fetchImpl = fetch, retryDelayMs = 2000 } = {}) {
  return {
    manifestStatus: (image, tag) =>
      withRetry(async () => {
        const tokRes = await fetchImpl(
          `https://ghcr.io/token?scope=repository:${GHCR_OWNER}/${image}:pull`
        );
        if (!tokRes.ok) throw new Error(`GHCR token for ${image} -> HTTP ${tokRes.status}`);
        const { token } = await tokRes.json();
        const res = await fetchImpl(`https://ghcr.io/v2/${GHCR_OWNER}/${image}/manifests/${tag}`, {
          method: "HEAD",
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: [
              "application/vnd.oci.image.index.v1+json",
              "application/vnd.oci.image.manifest.v1+json",
              "application/vnd.docker.distribution.manifest.list.v2+json",
              "application/vnd.docker.distribution.manifest.v2+json",
            ].join(","),
          },
        });
        if (res.status >= 500 || res.status === 429) throw new Error(`GHCR ${image}:${tag} -> HTTP ${res.status}`);
        return res.status;
      }, 3, retryDelayMs),
  };
}

// ── Core ────────────────────────────────────────────────────────────────────

const MAX_PER_COMMIT_LOOKUPS = 30;

export async function resolveTarget({ inputSha, github, ghcr }) {
  const errors = [];
  const warnings = [];
  const input = (inputSha || "").trim();
  const head = await github.mainHead();
  let sha;
  let source;

  if (input) {
    sha = input;
    source = "input";
    if (!isFullSha(sha)) {
      return { ok: false, sha, source, head, errors: [`'${sha}' is not a 40-char lowercase hex commit SHA.`], warnings, images: [], skipped: [] };
    }
  } else {
    source = "newest-built";
    const [mainShas, built] = await Promise.all([github.mainShas(), github.builtShas()]);
    sha = pickNewestBuilt(mainShas, built);
    if (!sha) {
      return {
        ok: false, sha: null, source, head, warnings, images: [], skipped: [],
        errors: [`No commit among the last ${mainShas.length} on grove-sites main has a successful '${DOCKER_WORKFLOW}' run. Pass target_sha explicitly.`],
      };
    }
  }

  // Image gate -- every tag must exist, in all cases.
  const images = [];
  for (const image of STOREFRONT_IMAGES) {
    const status = await ghcr.manifestStatus(image, sha);
    images.push({ image, status, ok: status === 200 });
    if (status !== 200) {
      errors.push(
        status === 404
          ? `ghcr.io/${GHCR_OWNER}/${image}:${sha} NOT FOUND (404) -- grove-sites docker.yml never published this SHA (path-filtered commit, or its build failed/was cancelled).`
          : `ghcr.io/${GHCR_OWNER}/${image}:${sha} returned HTTP ${status} (expected 200).`
      );
    }
  }

  // Skipped commits: what's on main HEAD that this promotion will NOT ship.
  let skipped = [];
  let compareStatus = "identical";
  if (sha !== head) {
    const cmp = await github.compare(sha, head);
    compareStatus = cmp.status;
    if (cmp.status === "diverged" || cmp.status === "behind") {
      warnings.push(`target ${sha.slice(0, 8)} is not an ancestor of grove-sites main (compare status: ${cmp.status}).`);
    }
    const perCommit = cmp.commits.length <= MAX_PER_COMMIT_LOOKUPS;
    for (const c of cmp.commits.slice().reverse()) {
      const hits = perCommit ? appCodeHits(await github.commitFiles(c.sha)) : null;
      skipped.push({ ...c, appHits: hits });
    }
    const rangeHits = appCodeHits(cmp.files);
    if (rangeHits.length > 0 || cmp.filesTruncated) {
      warnings.push(
        `${cmp.commits.length} skipped main commit(s) ${rangeHits.length ? "TOUCH APP CODE" : "may touch app code (file list truncated)"} ` +
          `(${rangeHits.slice(0, 5).join(", ")}${rangeHits.length > 5 ? ", ..." : ""}). ` +
          `Those changes will NOT ship in this promotion -- main HEAD's Docker build is likely still running or failed. ` +
          `Check https://github.com/${SITES_REPO}/actions/workflows/${DOCKER_WORKFLOW}?query=branch%3Amain before approving.`
      );
    }
  }

  return { ok: errors.length === 0, sha, source, head, compareStatus, images, skipped, errors, warnings };
}

// Commit subjects are third-party text going into a markdown table cell:
// escape backslashes FIRST (else a trailing `\` would eat our `\|`), then
// pipes, and flatten any newline so the row can't be split.
export const mdCell = (s) => String(s).replace(/\\/g, "\\\\").replace(/\|/g, "\\|").replace(/[\r\n]+/g, " ");

export function renderSummary(r) {
  const short = (s) => (s ? s.slice(0, 8) : "(none)");
  const out = ["## Build resolution", ""];
  out.push(
    r.source === "input"
      ? `Target \`${r.sha}\` was given explicitly.`
      : `\`target_sha\` was blank -- resolved to the newest grove-sites \`main\` commit with a successful \`${DOCKER_WORKFLOW}\` run: \`${r.sha}\`.`
  );
  out.push("", `grove-sites \`main\` HEAD: \`${r.head}\``, "");
  if (r.images.length) {
    out.push("| Image | Tag present |", "|---|---|");
    for (const i of r.images) out.push(`| \`ghcr.io/${GHCR_OWNER}/${i.image}:${short(r.sha)}…\` | ${i.ok ? "yes (200)" : `**NO (${i.status})**`} |`);
    out.push("");
  }
  if (r.sha && r.sha === r.head) {
    out.push("Target **is** main HEAD -- no commits skipped.");
  } else if (r.skipped.length) {
    out.push(`### ${r.skipped.length} main commit(s) skipped (newer than target, NOT shipped)`, "");
    out.push("| Commit | Subject | Touches app code? |", "|---|---|---|");
    for (const c of r.skipped) {
      const app = c.appHits === null ? "not checked (range too long)" : c.appHits.length ? `**YES** (\`${c.appHits[0]}\`${c.appHits.length > 1 ? ` +${c.appHits.length - 1}` : ""})` : "no";
      out.push(`| [\`${short(c.sha)}\`](https://github.com/${SITES_REPO}/commit/${c.sha}) | ${mdCell(c.subject)} | ${app} |`);
    }
  }
  for (const w of r.warnings) out.push("", `> [!WARNING]\n> ${w}`);
  for (const e of r.errors) out.push("", `> [!CAUTION]\n> ${e}`);
  out.push("");
  return out.join("\n");
}

// ── CLI ─────────────────────────────────────────────────────────────────────
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const github = makeGithub({ token: process.env.GH_TOKEN || process.env.GITHUB_TOKEN || "" });
  const ghcr = makeGhcr();
  try {
    const r = await resolveTarget({ inputSha: process.env.INPUT_SHA, github, ghcr });
    const summary = renderSummary(r);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary + "\n");
    else process.stdout.write(summary + "\n");
    for (const w of r.warnings) console.log(`::warning::${w}`);
    for (const e of r.errors) console.log(`::error::${e}`);
    if (!r.ok) process.exit(1);
    if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `target_sha=${r.sha}\n`);
    console.log(`Resolved target_sha=${r.sha} (${r.source}); all ${r.images.length} storefront images present.`);
  } catch (e) {
    console.log(`::error::storefront target resolution failed: ${e.message}`);
    process.exit(1);
  }
}
