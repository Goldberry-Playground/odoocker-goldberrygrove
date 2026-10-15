#!/usr/bin/env node
// Regression test for promote-storefronts.yml's reconcile branch (GOL-3409).
//
// What it protects:
//   The `promote` job checks out `github.sha` as of DISPATCH time, then waits
//   on the `production` Environment gate -- for run 38621755196 that wait was
//   ~16 hours (dispatched 2026-10-14 19:12Z, approved 10-15 10:59Z). The old
//   bump step cut the reconcile branch from THAT checkout, so by push time the
//   branch carried every commit main had gained meanwhile. One of them (#893)
//   touched .github/workflows/prod-modules-pin-drift.yml, and the App token
//   has no `workflows` permission, so the push was rejected:
//
//     refusing to allow a GitHub App to create or update workflow
//     .github/workflows/prod-modules-pin-drift.yml without workflows permission
//
//   -- AFTER the targeted apply + four redeploys had already succeeded. Prod
//   rolled, committed HCL stayed behind, the fingerprint verify and drift
//   re-check never ran, and the reconcile PR had to be opened by hand (#909).
//
//   The fix cuts the branch from a FRESH `git fetch origin main` in a throwaway
//   worktree at PR time, applies the one-file pin bump there via
//   scripts/bump-storefront-pin.sh, and asserts the branch-vs-main delta is
//   exactly variables.tf before pushing. This file pins both halves:
//
//     1. STATIC  -- the bump step no longer creates a branch from the
//                   dispatch-time checkout; the reconcile step fetches origin
//                   main BEFORE it pushes, builds from FETCH_HEAD, never
//                   merges/rebases, and carries the one-file assertion.
//     2. BEHAVIORAL -- the reconcile `run:` block, lifted from the workflow and
//                   run by a real bash against a local bare origin whose main
//                   moved a workflow file AFTER the runner's checkout, pushes a
//                   branch whose delta vs CURRENT main is only variables.tf
//                   (custom_modules_ref untouched). A negative control proves
//                   the OLD branch-from-checkout shape fails that assertion,
//                   plus the already-reconciled no-op path and the push-
//                   rejected path (clear ::error + RECONCILE_PR_URL=FAILED).
//
// node builtins only -- run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, chmodSync, readFileSync, mkdirSync, copyFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const workflowPath = join(repoRoot, ".github", "workflows", "promote-storefronts.yml");
const bumpScript = join(repoRoot, "scripts", "bump-storefront-pin.sh");
const TF_DIR = "infra/terraform/environments/production";
const VARS = `${TF_DIR}/variables.tf`;

const OLD_SHA = "4d25202ec9b3993633afeec8f48e81b6445ea63f";
const NEW_SHA = "c5e6b43c8d6496324fd4ae96a73b83886e0a9dfb";
const MODULES_SHA = "da63b7897793dbf2204c7aa5184657bdd1fabce5";
const RUN_ID = "38621755196";
const BRANCH = `promote-pin-${NEW_SHA.slice(0, 12)}-${RUN_ID}`;

let failures = 0;
const check = (name, ok, detail) => {
  if (ok) {
    console.log(`  ok   ${name}`);
  } else {
    failures++;
    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ""}`);
  }
};

// ── Minimal `run: |` block extractor (no YAML parser available) ───────────
const extractRunBlocks = (text) => {
  const lines = text.split("\n");
  const blocks = [];
  let stepName = null;
  for (let i = 0; i < lines.length; i++) {
    const nameMatch = lines[i].match(/^\s*-\s+name:\s*(.+?)\s*$/);
    if (nameMatch) stepName = nameMatch[1];
    const runMatch = lines[i].match(/^(\s*)run:\s*\|-?\s*$/);
    if (!runMatch) continue;
    const indent = runMatch[1].length;
    const body = [];
    let j = i + 1;
    for (; j < lines.length; j++) {
      if (lines[j].trim() === "") { body.push(""); continue; }
      if (lines[j].match(/^(\s*)/)[1].length <= indent) break;
      body.push(lines[j]);
    }
    const bodyIndent = Math.min(...body.filter((l) => l.trim()).map((l) => l.match(/^(\s*)/)[1].length));
    blocks.push({ step: stepName, body: body.map((l) => l.slice(bodyIndent)).join("\n") });
    i = j - 1;
  }
  return blocks;
};

const wf = readFileSync(workflowPath, "utf8");
const blocks = extractRunBlocks(wf);
const bump = blocks.find((b) => /^Bump the pinned image-tag SHA/.test(b.step || ""));
const recon = blocks.find((b) => /^Open reconcile PR/.test(b.step || ""));

// ── 1. STATIC ─────────────────────────────────────────────────────────────
console.log("static: promote-storefronts.yml step shape");
check("bump step found", !!bump);
check("reconcile step found", !!recon);
check("bump step calls scripts/bump-storefront-pin.sh", !!bump && /scripts\/bump-storefront-pin\.sh/.test(bump.body));
check("bump step does NOT cut a branch from the dispatch-time checkout",
  !!bump && !/checkout\s+-b|git\s+commit|git\s+push/.test(bump.body),
  "a branch created here is cut from github.sha as of dispatch, hours before the push");
if (recon) {
  const fetchAt = recon.body.search(/git fetch[^\n]*origin main/);
  const pushAt = recon.body.search(/git -C "\$RECON" push origin/);
  check("reconcile step fetches origin main", fetchAt >= 0);
  check("reconcile step pushes (from the worktree)", pushAt >= 0);
  check("fetch happens BEFORE the push", fetchAt >= 0 && pushAt >= 0 && fetchAt < pushAt);
  check("fetched sha is pinned once (FETCH_HEAD is per-worktree, invisible inside $RECON)", /BASE="\$\(git rev-parse FETCH_HEAD\)"/.test(recon.body));
  check("worktree is built from that fresh-main sha", /git worktree add --detach "\$RECON" "\$BASE"/.test(recon.body));
  check("reconcile step applies scripts/bump-storefront-pin.sh", /bump-storefront-pin\.sh/.test(recon.body));
  check("one-file delta assertion vs the fresh-main sha is present", /diff --name-only "\$BASE" HEAD/.test(recon.body));
  check("reconcile step never merges or rebases older history", !/git (-C "\$RECON" )?(merge|rebase)\b/.test(recon.body));
  check("push failure path writes RECONCILE_PR_URL=FAILED for the Discord step",
    /RECONCILE_PR_URL=FAILED/.test(recon.body) && /push origin[^\n]*\|\| fail/.test(recon.body));
}
const reconStepYaml = wf.slice(wf.indexOf("- name: Open reconcile PR"), wf.indexOf("- name: Verify build fingerprint"));
check("reconcile step runs under `shell: bash` (-eo pipefail), not the default -e-only shell",
  /\n\s*shell: bash\s*\n/.test(reconStepYaml));

// ── 2. BEHAVIORAL ─────────────────────────────────────────────────────────
const git = (cwd, ...args) => {
  const r = spawnSync("git", args, { cwd, encoding: "utf8", env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null", HOME: cwd } });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")} in ${cwd} failed:\n${r.stdout}${r.stderr}`);
  return r.stdout.trim();
};
const AUTHOR = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null"];
const commitAll = (cwd, msg) => { git(cwd, "add", "-A"); git(cwd, ...AUTHOR, "commit", "-q", "-m", msg); };

const varsTf = (hub, tenant, modules) => `variable "custom_modules_ref" {
  description = "grove-odoo-modules git-sync pin (a DIFFERENT repo)"
  type        = string
  default     = "${modules}"
}

variable "hub_image_tag" {
  description = "grove-hub image tag"
  type        = string
  default     = "${hub}"
}

variable "tenant_image_tag" {
  description = "tenant image tag"
  type        = string
  default     = "${tenant}"
}
`;

// Fixture: bare origin with main at A (old pin + a workflow file), a "runner"
// clone of A, then B pushed to origin main that edits the workflow file --
// exactly what #893 did while run 38621755196 sat on the Environment gate.
const makeFixture = () => {
  const root = mkdtempSync(join(tmpdir(), "gol3409-"));
  const origin = join(root, "origin.git");
  const seed = join(root, "seed");
  const runner = join(root, "runner");
  git(root, "init", "-q", "--bare", "-b", "main", origin);
  git(root, "init", "-q", "-b", "main", seed);
  mkdirSync(join(seed, TF_DIR), { recursive: true });
  mkdirSync(join(seed, ".github", "workflows"), { recursive: true });
  mkdirSync(join(seed, "scripts"), { recursive: true });
  writeFileSync(join(seed, VARS), varsTf(OLD_SHA, OLD_SHA, MODULES_SHA));
  writeFileSync(join(seed, ".github/workflows/prod-modules-pin-drift.yml"), "name: drift\non: push\njobs: {}\n");
  copyFileSync(bumpScript, join(seed, "scripts/bump-storefront-pin.sh"));
  chmodSync(join(seed, "scripts/bump-storefront-pin.sh"), 0o755);
  commitAll(seed, "A: baseline (old pin)");
  git(seed, "remote", "add", "origin", origin);
  git(seed, "push", "-q", "origin", "main");
  git(root, "clone", "-q", origin, runner);            // the runner's dispatch-time checkout = A
  // main moves on: B touches a workflow file (the App token cannot push that).
  writeFileSync(join(seed, ".github/workflows/prod-modules-pin-drift.yml"), "name: drift\non: push\njobs: {}\n# GOL-3328 guard\n");
  commitAll(seed, "B: ci(pin-drift): guard the Discord-step grep (#893)");
  git(seed, "push", "-q", "origin", "main");
  return { root, origin, seed, runner };
};

const runRecon = (fx, { targetSha = NEW_SHA } = {}) => {
  const bin = join(fx.root, "bin");
  mkdirSync(bin, { recursive: true });
  // `gh` stub: the real one needs a token + network; we only need its URL.
  writeFileSync(join(bin, "gh"), "#!/usr/bin/env bash\necho \"https://github.com/example/repo/pull/999\"\n");
  chmodSync(join(bin, "gh"), 0o755);
  const ghEnv = join(fx.root, `github_env_${Math.random().toString(36).slice(2)}`);
  writeFileSync(ghEnv, "");
  const r = spawnSync("bash", ["-eo", "pipefail", "-c", recon.body], {
    cwd: fx.runner,
    encoding: "utf8",
    env: {
      PATH: `${bin}:${process.env.PATH}`,
      HOME: fx.root, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null",
      TF_DIR, TARGET_SHA: targetSha, GITHUB_RUN_ID: RUN_ID,
      GITHUB_ENV: ghEnv, GITHUB_SERVER_URL: "https://github.com", GITHUB_REPOSITORY: "example/repo",
      GH_TOKEN: "stub",
    },
  });
  return { ...r, githubEnv: readFileSync(ghEnv, "utf8") };
};

if (recon) {
  console.log("behavioral: reconcile branch is a one-file delta vs CURRENT main");
  const fx = makeFixture();
  const r = runRecon(fx);
  check("reconcile block exits 0", r.status === 0, `${r.stdout}\n${r.stderr}`);
  let delta = null, branchVars = null, branchHasB = false;
  try {
    delta = git(fx.origin, "diff", "--name-only", `main..${BRANCH}`);
    branchVars = git(fx.origin, "show", `${BRANCH}:${VARS}`);
    branchHasB = git(fx.origin, "merge-base", "--is-ancestor", "main", BRANCH) === "";
  } catch (e) { check("pushed branch exists on origin", false, String(e.message)); }
  check("pushed branch delta vs origin/main is exactly variables.tf", delta === VARS, `got: ${JSON.stringify(delta)}`);
  check("pushed branch descends from CURRENT origin/main (B), not the dispatch-time checkout (A)", branchHasB);
  check("hub_image_tag + tenant_image_tag bumped on the branch",
    !!branchVars && (branchVars.match(new RegExp(NEW_SHA, "g")) || []).length === 2 && !branchVars.includes(OLD_SHA));
  check("custom_modules_ref untouched on the branch (GOL-1708 finding 1)", !!branchVars && branchVars.includes(MODULES_SHA));
  check("RECONCILE_PR_URL exported for the Discord step", /^RECONCILE_PR_URL=https:\/\/github\.com\/example\/repo\/pull\/999$/m.test(r.githubEnv), r.githubEnv);
  check("runner working tree left on its original checkout (apply already read it)",
    git(fx.runner, "rev-parse", "HEAD") === git(fx.origin, "rev-parse", "main^"));

  // Negative control: the OLD shape (branch cut from the dispatch-time
  // checkout + same edit) carries the workflow delta -- the exact push GitHub
  // rejected. Proves the one-file assertion actually discriminates.
  console.log("negative control: a branch cut from the dispatch-time checkout drags the workflow change along");
  const old = join(fx.root, "old-shape");
  git(fx.root, "clone", "-q", fx.origin, old);
  git(old, "checkout", "-q", "main^");                  // = A, the dispatch-time sha
  spawnSync(bumpScript, [join(old, VARS), NEW_SHA], { encoding: "utf8" });
  git(old, "checkout", "-q", "-b", "old-shape");
  commitAll(old, "old shape");
  const oldDelta = git(old, "diff", "--name-only", "origin/main", "HEAD").split("\n").sort();
  check("old-shape branch vs current main touches the workflow file too",
    oldDelta.includes(".github/workflows/prod-modules-pin-drift.yml") && oldDelta.includes(VARS), oldDelta.join(","));

  console.log("behavioral: already reconciled upstream -> no branch, clear no-op");
  const fx2 = makeFixture();
  writeFileSync(join(fx2.seed, VARS), varsTf(NEW_SHA, NEW_SHA, MODULES_SHA));
  commitAll(fx2.seed, "C: reconcile by hand (#909)");
  git(fx2.seed, "push", "-q", "origin", "main");
  const r2 = runRecon(fx2);
  check("exits 0 when origin/main already pins the target", r2.status === 0, `${r2.stdout}\n${r2.stderr}`);
  check("no branch pushed in the no-op case", git(fx2.origin, "branch", "--list", BRANCH) === "");
  check("RECONCILE_PR_URL says none/already pinned", /^RECONCILE_PR_URL=\(none -- main already pins/m.test(r2.githubEnv), r2.githubEnv);

  console.log("behavioral: push rejected -> loud ::error, FAILED marker for Discord, exit 1");
  const fx3 = makeFixture();
  mkdirSync(join(fx3.origin, "hooks"), { recursive: true });
  writeFileSync(join(fx3.origin, "hooks", "pre-receive"), "#!/usr/bin/env bash\necho 'refusing to allow a GitHub App to create or update workflow (simulated)' >&2\nexit 1\n");
  chmodSync(join(fx3.origin, "hooks", "pre-receive"), 0o755);
  const r3 = runRecon(fx3);
  check("exits non-zero when the push is rejected", r3.status !== 0);
  check("::error names the by-hand recovery (file + target sha)",
    /::error::Roll SUCCEEDED/.test(r3.stdout) && r3.stdout.includes(VARS) && r3.stdout.includes(NEW_SHA), r3.stdout);
  check("RECONCILE_PR_URL=FAILED exported so the Discord embed says so", /^RECONCILE_PR_URL=FAILED \(pushing branch/m.test(r3.githubEnv), r3.githubEnv);
}

// ── bump script contract ──────────────────────────────────────────────────
console.log("scripts/bump-storefront-pin.sh contract");
{
  const d = mkdtempSync(join(tmpdir(), "gol3409-bump-"));
  const f = join(d, "variables.tf");
  writeFileSync(f, varsTf(OLD_SHA, OLD_SHA, MODULES_SHA));
  const a = spawnSync(bumpScript, [f, NEW_SHA], { encoding: "utf8" });
  check("rc=0 on change", a.status === 0, a.stderr);
  const after = readFileSync(f, "utf8");
  check("only hub/tenant defaults rewritten", (after.match(new RegExp(NEW_SHA, "g")) || []).length === 2 && after.includes(MODULES_SHA));
  const b = spawnSync(bumpScript, [f, NEW_SHA], { encoding: "utf8" });
  check("rc=3 when already at target (idempotent no-op)", b.status === 3, `${b.stdout}${b.stderr}`);
  const c = spawnSync(bumpScript, [f, "latest"], { encoding: "utf8" });
  check("rc=1 on a non-40-hex target", c.status === 1 && /not a 40-char/.test(c.stderr));
}

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
