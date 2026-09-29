#!/usr/bin/env node
// Regression test for scripts/discord-status.sh's ARGUMENT CONTRACT (GOL-2564).
//
// What it protects:
//   discord-status.sh is the single alerting path for every Grove watcher. It
//   is always invoked as `... || true`, because a failed Discord post must
//   never fail the workflow it is reporting on. That `|| true` also swallows
//   an ARG-VALIDATION failure, which is how `--branch` being in the required
//   set — while NEITHER caller passed it — silently disabled Discord alerting
//   in qa-health.yml and ci-failure-notify.yml for their entire lifetime. No
//   failure alerts, no @here escalation, no RECOVERED. Nobody noticed, because
//   a watcher that says nothing looks exactly like a watcher with nothing to
//   say.
//
//   So the important assertion here is the second one: every call site in
//   .github/workflows is checked against the required-arg list PARSED OUT OF
//   THE SCRIPT ITSELF. No copy of that list lives in this file, so the script
//   and its callers cannot drift apart again — adding a new required arg, or a
//   new caller that forgets one, fails here instead of going quiet in prod.
//
// node builtins only — run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, chmodSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const script = join(repoRoot, "scripts", "discord-status.sh");
const workflowDir = join(repoRoot, ".github", "workflows");

let failures = 0;
const check = (name, ok, detail) => {
  if (ok) {
    console.log(`  ok   ${name}`);
  } else {
    failures++;
    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ""}`);
  }
};

// ── Stubs: never touch the network ─────────────────────────────────────────
// `gh` records the args it was called with so we can assert --branch is
// forwarded when given and omitted when not. `curl` records that a POST was
// attempted, and succeeds, so the success path is distinguishable from the
// quiet-success path (which must not call curl at all).
const dir = mkdtempSync(join(tmpdir(), "discord-status-test-"));
const bin = join(dir, "bin");
spawnSync("mkdir", ["-p", bin]);

writeFileSync(
  join(bin, "gh"),
  `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> "$GH_CALLS"\nprintf 'success\\nsuccess\\n'\n`,
);
writeFileSync(
  join(bin, "curl"),
  `#!/usr/bin/env bash\necho posted >> "$CURL_CALLS"\nexit 0\n`,
);
chmodSync(join(bin, "gh"), 0o755);
chmodSync(join(bin, "curl"), 0o755);

let seq = 0;
function run(args) {
  seq++;
  const ghCalls = join(dir, `gh.${seq}`);
  const curlCalls = join(dir, `curl.${seq}`);
  const r = spawnSync("bash", [script, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      GH_CALLS: ghCalls,
      CURL_CALLS: curlCalls,
      DISCORD_OPS_WEBHOOK_URL: "https://discord.invalid/webhook",
      GITHUB_REPOSITORY: "Goldberry-Playground/odoocker-goldberrygrove",
    },
  });
  const read = (p) => {
    try {
      return readFileSync(p, "utf8");
    } catch {
      return "";
    }
  };
  return { code: r.status, out: `${r.stdout}${r.stderr}`, gh: read(ghCalls), curl: read(curlCalls) };
}

const BASE = ["--workflow=qa-health.yml", "--run-url=https://example.test/run/1", "--title=T"];

console.log("discord-status.sh argument contract");

// ── 1. The regression itself ───────────────────────────────────────────────
// Before the fix this exited 1 with "Missing --branch" and posted nothing.
const quiet = run(["--status=success", ...BASE, "--quiet-success"]);
check(
  "no --branch + success + --quiet-success -> exit 0, no POST",
  quiet.code === 0 && quiet.curl === "" && !/Missing --branch/.test(quiet.out),
  `exit=${quiet.code} curl=${JSON.stringify(quiet.curl)} out=${quiet.out.trim()}`,
);

const fail = run(["--status=failure", ...BASE]);
check(
  "no --branch + failure -> exit 0, POST attempted",
  fail.code === 0 && fail.curl.includes("posted"),
  `exit=${fail.code} curl=${JSON.stringify(fail.curl)} out=${fail.out.trim()}`,
);
check(
  "no --branch -> `gh run list` is NOT branch-filtered",
  fail.gh.includes("run list") && !fail.gh.includes("--branch"),
  `gh args: ${JSON.stringify(fail.gh)}`,
);

// ── 2. --branch still works when supplied ──────────────────────────────────
const branched = run(["--status=failure", ...BASE, "--branch=qa"]);
check(
  "--branch=qa -> forwarded to `gh run list`",
  branched.code === 0 && branched.gh.includes("--branch=qa"),
  `exit=${branched.code} gh args: ${JSON.stringify(branched.gh)}`,
);

// ── 3. The genuinely-required args are still enforced ──────────────────────
for (const omit of ["--status=success", "--workflow=qa-health.yml", "--run-url=https://example.test/run/1", "--title=T"]) {
  const args = ["--status=success", ...BASE].filter((a) => a !== omit);
  const r = run(args);
  check(`omitting ${omit.split("=")[0]} -> rejected (exit 1)`, r.code === 1, `exit=${r.code} out=${r.out.trim()}`);
}

// ── 4. THE GUARD: every call site supplies every required arg ──────────────
// Required list is parsed out of the script, never restated here.
const src = readFileSync(script, "utf8");
const m = src.match(/^for required in ([A-Z_ ]+); do$/m);
if (!m) {
  console.error("  FAIL could not parse the `for required in …` loop out of discord-status.sh");
  console.error("       If the validation was restructured, update this test deliberately.");
  process.exit(1);
}
const required = m[1].trim().split(/\s+/).map((v) => `--${v.toLowerCase().replace(/_/g, "-")}`);
console.log(`  (required args parsed from the script: ${required.join(", ")})`);

// Grab each real invocation: from the `discord-status.sh` line through the
// last backslash-continued line. Mentions inside YAML comments are skipped —
// several workflows explain the script in prose near the step that runs it,
// and a comment is not a call site.
function invocationsIn(body) {
  const out = [];
  const lines = body.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const idx = lines[i].indexOf("discord-status.sh");
    if (idx < 0) continue;
    if (lines[i].slice(0, idx).includes("#")) continue; // commented mention
    let block = lines[i];
    let j = i;
    while (/\\\s*$/.test(lines[j]) && j + 1 < lines.length) {
      j++;
      block += `\n${lines[j]}`;
    }
    out.push(block);
    i = j;
  }
  return out;
}

const callers = readdirSync(workflowDir)
  .filter((f) => f.endsWith(".yml") || f.endsWith(".yaml"))
  .map((f) => [f, invocationsIn(readFileSync(join(workflowDir, f), "utf8"))])
  .filter(([, invs]) => invs.length > 0);

check("at least one workflow calls discord-status.sh", callers.length > 0, "found none — did the path change?");

for (const [file, invocations] of callers) {
  for (const inv of invocations) {
    const missing = required.filter((a) => !inv.includes(`${a}=`) && !inv.includes(`${a} `));
    check(
      `${file}: invocation supplies every required arg`,
      missing.length === 0,
      `missing: ${missing.join(", ")}\n       in:\n${inv}`,
    );
  }
}

if (failures > 0) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("\nAll checks passed.");
