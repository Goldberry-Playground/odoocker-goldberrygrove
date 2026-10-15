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

// Shape of the real variables.tf: custom_modules_ref (a DIFFERENT repo's SHA)
// sits ABOVE the two storefront pins, each `default` inside its own block.
const variablesTf = (hub, tenant, modules = MODULES_PIN) => `variable "custom_modules_ref" {
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

const runReconcile = (cwd, branch, sha, env = {}) =>
  spawnSync("bash", [reconcile, branch, sha], { cwd, encoding: "utf8", env: { ...process.env, TF_DIR, ...env } });

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

  const r2 = spawnSync("bash", [bump, file, NEW_PIN], { encoding: "utf8" });
  check("second run exits 0", r2.status === 0, r2.stderr);
  check("second run prints nochange", r2.stdout.trim() === "nochange", r2.stdout);
  check("second run leaves the file byte-identical", readFileSync(file, "utf8") === tf);

  const r3 = spawnSync("bash", [bump, file, "not-a-sha"], { encoding: "utf8" });
  check("rejects a non-40-hex SHA", r3.status !== 0);
}

if (failures > 0) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
