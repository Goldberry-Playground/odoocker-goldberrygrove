#!/usr/bin/env node
// Behavioral tests for scripts/ci/promote-reconcile-branch.sh and
// scripts/ci/bump-storefront-pin.sh (promote-storefronts.yml, step 3b).
// Run: `node scripts/ci/promote-reconcile-branch.test.mjs`
//
// Replays the 2026-10-15 incident (run 38621755196). The promote job checks
// out `github.sha` -- main as it was when the run was DISPATCHED -- and the
// `production` Environment gate can hold it for hours. Main moved on while the
// run waited: #893 changed .github/workflows/prod-modules-pin-drift.yml eight
// minutes after dispatch. The reconcile branch was then cut from the stale
// dispatch SHA, so relative to main it carried the OLD copy of that workflow
// file, and GitHub refused the push: "refusing to allow a GitHub App to create
// or update workflow ... without `workflows` permission". The roll had already
// succeeded, so main was left behind live until a human opened the PR.
//
// The fix: build the reconcile commit on the CURRENT remote main (in a
// throwaway worktree) at push time, so the pushed branch differs from main by
// the two pin lines and nothing else. These tests drive the script as a black
// box against a local bare "origin"; no network, no token, no GitHub.
//
// node builtins only -- run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const reconcile = join(repoRoot, "scripts", "ci", "promote-reconcile-branch.sh");
const bump = join(repoRoot, "scripts", "ci", "bump-storefront-pin.sh");

for (const f of [reconcile, bump]) {
  if (!existsSync(f)) {
    console.error(`FAIL: ${f} does not exist.`);
    process.exit(1);
  }
}

let failures = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name}${detail ? `\n       ${detail}` : ""}`);
  }
};

const TF_DIR = "infra/terraform/environments/production";
const OLD_PIN = "4d25202ec9b3993633afeec8f48e81b6445ea63f";
const NEW_PIN = "c5e6b43c8d6496324fd4ae96a73b83886e0a9dfb";
const MODULES_PIN = "da63b7897793dbf2204c7aa5184657bdd1fabce5";
// consult_image_tag is an AgriforestryOS SHA (GOL-3723) -- a third repo's pin
// living in the same file, moved only when a consult SHA is passed.
const CONSULT_PIN = "01c34e2134f848fdfc3073c76b56f8cb142fb7a7";
const NEW_CONSULT_PIN = "fc67652a0d3e5b1c9f8e7d6c5b4a39281706f5e4";

// Shape of the real variables.tf: custom_modules_ref (a DIFFERENT repo's SHA)
// sits ABOVE the two storefront pins, each `default` inside its own block.
const variablesTf = (hub, tenant, modules = MODULES_PIN, consult = CONSULT_PIN) => `variable "custom_modules_ref" {
  description = "grove-odoo-modules git-sync pin (see var.odoo_image_tag)"
  type        = string
  default     = "${modules}"
}

variable "hub_image_tag" {
  description = "grove-hub image tag"
  type        = string
  default     = "${hub}"
}

variable "tenant_image_tag" {
  description = "grove-tenant image tag"
  type        = string
  default     = "${tenant}"
}

variable "consult_image_tag" {
  description = "grove-consult image tag (AgriforestryOS SHA)"
  type        = string
  default     = "${consult}"

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.consult_image_tag))
    error_message = "must be a 40-char sha"
  }
}
`;

const pinOf = (text, name) => {
  const m = text.match(new RegExp(`variable "${name}" \\{[^}]*default\\s*=\\s*"([0-9a-f]{40})"`));
  return m ? m[1] : null;
};

const git = (cwd, ...args) => {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")} failed in ${cwd}:\n${r.stderr}`);
  return r.stdout.trim();
};

const gitIdentity = ["-c", "user.name=test", "-c", "user.email=test@example.com"];
const commitAll = (cwd, msg) => {
  git(cwd, "add", "-A");
  git(cwd, ...gitIdentity, "commit", "-q", "-m", msg);
  return git(cwd, "rev-parse", "HEAD");
};

// Build: bare origin; a seed clone that authors main; a "runner" clone that
// (like actions/checkout with `sha: github.sha`) sits detached at an OLDER
// main commit. Returns the SHAs so tests can assert parentage.
function scenario({ mainPin = OLD_PIN, advanceMain = true } = {}) {
  const root = mkdtempSync(join(tmpdir(), "promote-reconcile-"));
  const origin = join(root, "origin.git");
  git(root, "init", "-q", "--bare", "--initial-branch=main", origin);

  const seed = join(root, "seed");
  git(root, "clone", "-q", origin, seed);
  mkdirSync(join(seed, TF_DIR), { recursive: true });
  mkdirSync(join(seed, ".github", "workflows"), { recursive: true });
  writeFileSync(join(seed, TF_DIR, "variables.tf"), variablesTf(mainPin, mainPin));
  writeFileSync(join(seed, ".github", "workflows", "prod-modules-pin-drift.yml"), "name: drift\non: workflow_dispatch\n");
  const dispatchSha = commitAll(seed, "A: state of main when the promote run was dispatched");
  git(seed, "push", "-q", "origin", "HEAD:main");

  // The runner's checkout: detached at the dispatch SHA, exactly as the
  // promote job sees the repo after the Environment gate releases it.
  const runner = join(root, "runner");
  git(root, "clone", "-q", origin, runner);
  git(runner, "checkout", "-q", "--detach", dispatchSha);

  let mainTip = dispatchSha;
  if (advanceMain) {
    // #893-shaped commit: a workflow file changes on main AFTER dispatch, while
    // the run is parked at the approval gate.
    writeFileSync(join(seed, ".github", "workflows", "prod-modules-pin-drift.yml"), "name: drift\non: workflow_dispatch\n# GOL-3328 guard\n");
    mainTip = commitAll(seed, "B: ci(pin-drift): landed on main while the promote run waited at the gate");
    git(seed, "push", "-q", "origin", "HEAD:main");
  }
  return { root, origin, runner, dispatchSha, mainTip };
}

const runReconcile = (cwd, branch, sha, env = {}, consultSha = "") =>
  spawnSync("bash", [reconcile, branch, sha, ...(consultSha ? [consultSha] : [])], { cwd, encoding: "utf8", env: { ...process.env, TF_DIR, ...env } });

console.log("promote-reconcile-branch.sh: incident replay (main moved while the run waited at the gate)");
{
  const s = scenario();
  const branch = "promote-pin-c5e6b43c8d64-38621755196";
  const r = runReconcile(s.runner, branch, NEW_PIN);
  check("exits 0", r.status === 0, r.stderr);
  check("reports result=pushed", /^result=pushed\b/m.test(r.stdout), r.stdout);

  const tip = git(s.origin, "rev-parse", `refs/heads/${branch}`);
  const parent = git(s.origin, "rev-parse", `${tip}^`);
  check("reconcile commit's parent is main's CURRENT tip (B), not the dispatch SHA (A)",
    parent === s.mainTip, `parent=${parent} mainTip=${s.mainTip} dispatch=${s.dispatchSha}`);
  const changed = git(s.origin, "diff", "--name-only", "main", tip).split("\n").filter(Boolean);
  check("branch differs from main by variables.tf ONLY (no workflow file in the diff)",
    changed.length === 1 && changed[0] === `${TF_DIR}/variables.tf`, `changed=${JSON.stringify(changed)}`);

  const tf = git(s.origin, "show", `${tip}:${TF_DIR}/variables.tf`);
  check("hub_image_tag bumped", pinOf(tf, "hub_image_tag") === NEW_PIN);
  check("tenant_image_tag bumped", pinOf(tf, "tenant_image_tag") === NEW_PIN);
  check("custom_modules_ref untouched (GOL-1708 finding 1)", pinOf(tf, "custom_modules_ref") === MODULES_PIN);
  check("consult_image_tag untouched when no consult SHA is passed (GOL-3723)", pinOf(tf, "consult_image_tag") === CONSULT_PIN);

  const msg = git(s.origin, "log", "-1", "--format=%s", tip);
  check("commit subject names the promoted SHA", msg.includes(NEW_PIN), msg);

  // The runner's own checkout is what the targeted terraform apply reads; the
  // script must leave it alone (HEAD still at the dispatch SHA, no stray
  // worktree left behind).
  check("runner HEAD still at the dispatch SHA", git(s.runner, "rev-parse", "HEAD") === s.dispatchSha);
  const worktrees = git(s.runner, "worktree", "list", "--porcelain").split("worktree ").filter(Boolean);
  check("throwaway worktree cleaned up", worktrees.length === 1, git(s.runner, "worktree", "list"));
}

console.log("promote-reconcile-branch.sh: main already carries the pin (hand-merged while the run waited)");
{
  const s = scenario({ mainPin: NEW_PIN });
  const branch = "promote-pin-c5e6b43c8d64-00000000001";
  const r = runReconcile(s.runner, branch, NEW_PIN);
  check("exits 0", r.status === 0, r.stderr);
  check("reports result=nochange", /^result=nochange\b/m.test(r.stdout), r.stdout);
  const ls = spawnSync("git", ["rev-parse", "--verify", "-q", `refs/heads/${branch}`], { cwd: s.origin, encoding: "utf8" });
  check("no branch pushed", ls.status !== 0, ls.stdout);
}

console.log("promote-reconcile-branch.sh: consult SHA passed (promote ran with consult_tag, GOL-3723)");
{
  const s = scenario();
  const branch = "promote-pin-c5e6b43c8d64-00000000002";
  const r = runReconcile(s.runner, branch, NEW_PIN, {}, NEW_CONSULT_PIN);
  check("exits 0", r.status === 0, r.stderr);
  check("reports result=pushed", /^result=pushed\b/m.test(r.stdout), r.stdout);
  const tip = git(s.origin, "rev-parse", `refs/heads/${branch}`);
  const tf = git(s.origin, "show", `${tip}:${TF_DIR}/variables.tf`);
  check("hub_image_tag bumped", pinOf(tf, "hub_image_tag") === NEW_PIN);
  check("tenant_image_tag bumped", pinOf(tf, "tenant_image_tag") === NEW_PIN);
  check("consult_image_tag bumped to the consult SHA, not the storefront SHA", pinOf(tf, "consult_image_tag") === NEW_CONSULT_PIN);
  check("custom_modules_ref untouched", pinOf(tf, "custom_modules_ref") === MODULES_PIN);
  const msg = git(s.origin, "log", "-1", "--format=%s", tip);
  check("commit subject names both SHAs", msg.includes(NEW_PIN) && msg.includes(NEW_CONSULT_PIN), msg);
}

console.log("promote-reconcile-branch.sh: consult-only move (storefront pin already on main)");
{
  const s = scenario({ mainPin: NEW_PIN });
  const branch = "promote-pin-c5e6b43c8d64-00000000003";
  const r = runReconcile(s.runner, branch, NEW_PIN, {}, NEW_CONSULT_PIN);
  check("exits 0", r.status === 0, r.stderr);
  check("reports result=pushed (the consult pin still has to reconcile)", /^result=pushed\b/m.test(r.stdout), r.stdout);
  const tip = git(s.origin, "rev-parse", `refs/heads/${branch}`);
  const tf = git(s.origin, "show", `${tip}:${TF_DIR}/variables.tf`);
  check("consult_image_tag bumped", pinOf(tf, "consult_image_tag") === NEW_CONSULT_PIN);
  check("hub_image_tag unchanged", pinOf(tf, "hub_image_tag") === NEW_PIN);
}

console.log("promote-reconcile-branch.sh: rejects a non-SHA consult target before touching the remote");
{
  const s = scenario();
  const r = runReconcile(s.runner, "promote-pin-bad-2", NEW_PIN, {}, "latest");
  check("exits non-zero", r.status !== 0);
  const ls = spawnSync("git", ["rev-parse", "--verify", "-q", "refs/heads/promote-pin-bad-2"], { cwd: s.origin, encoding: "utf8" });
  check("no branch pushed", ls.status !== 0);
}

console.log("promote-reconcile-branch.sh: rejects a non-SHA target before touching the remote");
{
  const s = scenario();
  const r = runReconcile(s.runner, "promote-pin-bad-1", "main");
  check("exits non-zero", r.status !== 0);
  const ls = spawnSync("git", ["rev-parse", "--verify", "-q", "refs/heads/promote-pin-bad-1"], { cwd: s.origin, encoding: "utf8" });
  check("no branch pushed", ls.status !== 0);
}

console.log("bump-storefront-pin.sh: anchored edit (the GOL-1708 custom_modules_ref trap)");
{
  const dir = mkdtempSync(join(tmpdir(), "bump-pin-"));
  const file = join(dir, "variables.tf");
  // custom_modules_ref deliberately shares the OLD storefront SHA: an
  // unanchored replace would rewrite it too.
  writeFileSync(file, variablesTf(OLD_PIN, OLD_PIN, OLD_PIN));
  const r1 = spawnSync("bash", [bump, file, NEW_PIN], { encoding: "utf8" });
  check("first run exits 0", r1.status === 0, r1.stderr);
  check("first run prints changed", r1.stdout.trim() === "changed", r1.stdout);
  const tf = readFileSync(file, "utf8");
  check("hub_image_tag bumped", pinOf(tf, "hub_image_tag") === NEW_PIN);
  check("tenant_image_tag bumped", pinOf(tf, "tenant_image_tag") === NEW_PIN);
  check("custom_modules_ref left at the shared old SHA", pinOf(tf, "custom_modules_ref") === OLD_PIN);
  check("consult_image_tag left alone without a third argument", pinOf(tf, "consult_image_tag") === CONSULT_PIN);

  const r2 = spawnSync("bash", [bump, file, NEW_PIN], { encoding: "utf8" });
  check("second run exits 0", r2.status === 0, r2.stderr);
  check("second run prints nochange", r2.stdout.trim() === "nochange", r2.stdout);
  check("second run leaves the file byte-identical", readFileSync(file, "utf8") === tf);

  const r3 = spawnSync("bash", [bump, file, "not-a-sha"], { encoding: "utf8" });
  check("rejects a non-40-hex SHA", r3.status !== 0);
}

console.log("bump-storefront-pin.sh: optional consult pin (GOL-3723)");
{
  const dir = mkdtempSync(join(tmpdir(), "bump-pin-consult-"));
  const file = join(dir, "variables.tf");
  // The consult pin deliberately shares the OLD storefront SHA: only the
  // block anchor keeps a storefront-only bump from rewriting it.
  writeFileSync(file, variablesTf(OLD_PIN, OLD_PIN, MODULES_PIN, OLD_PIN));
  const r1 = spawnSync("bash", [bump, file, NEW_PIN], { encoding: "utf8" });
  check("storefront-only bump exits 0", r1.status === 0, r1.stderr);
  check("consult pin sharing the old storefront SHA is NOT rewritten", pinOf(readFileSync(file, "utf8"), "consult_image_tag") === OLD_PIN);

  const r2 = spawnSync("bash", [bump, file, NEW_PIN, NEW_CONSULT_PIN], { encoding: "utf8" });
  check("consult-only move prints changed", r2.status === 0 && r2.stdout.trim() === "changed", r2.stdout + r2.stderr);
  const tf = readFileSync(file, "utf8");
  check("consult_image_tag bumped", pinOf(tf, "consult_image_tag") === NEW_CONSULT_PIN);
  check("storefront pins not given the consult SHA", pinOf(tf, "hub_image_tag") === NEW_PIN && pinOf(tf, "tenant_image_tag") === NEW_PIN);

  const r3 = spawnSync("bash", [bump, file, NEW_PIN, NEW_CONSULT_PIN], { encoding: "utf8" });
  check("re-run prints nochange", r3.status === 0 && r3.stdout.trim() === "nochange", r3.stdout);
  const r4 = spawnSync("bash", [bump, file, NEW_PIN, ""], { encoding: "utf8" });
  check("empty third argument = storefront-only (nochange, exits 0)", r4.status === 0 && r4.stdout.trim() === "nochange", r4.stdout + r4.stderr);
  const r5 = spawnSync("bash", [bump, file, NEW_PIN, "latest"], { encoding: "utf8" });
  check("rejects a non-40-hex consult SHA", r5.status !== 0);
  check("rejected run leaves the file untouched", readFileSync(file, "utf8") === tf);

  // A variables.tf from before GOL-3723 (no consult block) must still bump.
  const legacy = join(dir, "legacy.tf");
  writeFileSync(legacy, variablesTf(OLD_PIN, OLD_PIN).replace(/\nvariable "consult_image_tag"[\s\S]*$/, ""));
  const r6 = spawnSync("bash", [bump, legacy, NEW_PIN], { encoding: "utf8" });
  check("file without a consult block still bumps storefront pins", r6.status === 0 && r6.stdout.trim() === "changed", r6.stderr);
  const r7 = spawnSync("bash", [bump, legacy, NEW_PIN, NEW_CONSULT_PIN], { encoding: "utf8" });
  check("consult SHA against a file without the block fails loudly", r7.status !== 0);
}

if (failures > 0) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
