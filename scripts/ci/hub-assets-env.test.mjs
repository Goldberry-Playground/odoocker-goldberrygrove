#!/usr/bin/env node
// Behavioral tests for scripts/ci/hub-assets-env.sh (promote-storefronts.yml).
// Run: `node scripts/ci/hub-assets-env.test.mjs`
//
// Replays the 2026-11-18 incident: the Train #5 promote passed the literal
// "ci-promote-placeholder" for the three grove_assets_* variables, and because
// the promote apply targets digitalocean_app.hub, the hub's real Spaces
// credentials and optimize bearer were overwritten with that string for six
// hours. The script under test reads the hub's live values and hands them
// back to Terraform, and refuses to run when the live values are not usable.
//
// Also guards the workflow itself: promote-storefronts.yml must call the
// script and must not set a grove_assets_* variable to a literal again.
//
// node builtins only -- run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const script = join(repoRoot, "scripts", "ci", "hub-assets-env.sh");
const workflow = join(repoRoot, ".github", "workflows", "promote-storefronts.yml");

if (!existsSync(script)) {
  console.error(`FAIL: ${script} does not exist.`);
  process.exit(1);
}

let failures = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ""}`);
  }
};

// Fake values only; nothing here is a real credential.
const GOOD = {
  GROVE_ASSETS_KEY: "FAKEKEY0000000000001",
  GROVE_ASSETS_SECRET: "fake/secret+with=symbols",
  GROVE_ASSETS_OPTIMIZE_TOKEN: "fake-optimize-token-0123456789abcdef",
};

const hubApp = (envs, name = "grove-hub-prod") => ({
  id: "00000000-0000-0000-0000-000000000001",
  spec: {
    name,
    services: [
      {
        name: "hub",
        envs: [
          { key: "ODOO_API_KEY", value: "unrelated", scope: "RUN_TIME" },
          ...Object.entries(envs).map(([key, value]) => ({ key, value, scope: "RUN_TIME" })),
        ],
      },
    ],
  },
});
const otherApp = { id: "x", spec: { name: "grove-nursery-prod", services: [{ name: "web", envs: [] }] } };

const run = (payload) => {
  const dir = mkdtempSync(join(tmpdir(), "hub-assets-env-"));
  const input = join(dir, "apps.json");
  const out = join(dir, "env");
  writeFileSync(input, JSON.stringify(payload));
  writeFileSync(out, "EXISTING=1\n");
  const r = spawnSync("bash", [script, input, out], { encoding: "utf8" });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, env: readFileSync(out, "utf8") };
};

console.log("carries live values through");
for (const [label, payload] of [
  ["API body {apps:[...]}", { apps: [otherApp, hubApp(GOOD)] }],
  ["doctl bare array", [otherApp, hubApp(GOOD)]],
]) {
  const r = run(payload);
  check(`${label}: exits 0`, r.status === 0, `status ${r.status} ${r.stderr}`);
  check(
    `${label}: writes the three TF_VARs after existing content`,
    r.env ===
      "EXISTING=1\n" +
        `TF_VAR_grove_assets_key=${GOOD.GROVE_ASSETS_KEY}\n` +
        `TF_VAR_grove_assets_secret=${GOOD.GROVE_ASSETS_SECRET}\n` +
        `TF_VAR_grove_assets_optimize_token=${GOOD.GROVE_ASSETS_OPTIMIZE_TOKEN}\n`,
  );
  const unmasked = r.stdout
    .split("\n")
    .filter((l) => !l.startsWith("::add-mask::"))
    .join("\n");
  check(
    `${label}: every value is masked and appears nowhere else in the output`,
    Object.values(GOOD).every(
      (v) => r.stdout.includes(`::add-mask::${v}`) && !unmasked.includes(v) && !r.stderr.includes(v),
    ),
  );
}

console.log("fails closed on unusable live values");
const badCases = [
  ["the 2026-11-18 placeholder", { ...GOOD, GROVE_ASSETS_SECRET: "ci-promote-placeholder" }, "GROVE_ASSETS_SECRET"],
  ["any placeholder, case-insensitive", { ...GOOD, GROVE_ASSETS_KEY: "CI-Plan-PLACEHOLDER" }, "GROVE_ASSETS_KEY"],
  ["an empty value", { ...GOOD, GROVE_ASSETS_OPTIMIZE_TOKEN: "" }, "GROVE_ASSETS_OPTIMIZE_TOKEN"],
  ["an encrypted EV[...] value", { ...GOOD, GROVE_ASSETS_SECRET: "EV[1:abc:def]" }, "GROVE_ASSETS_SECRET"],
  ["a multi-line value", { ...GOOD, GROVE_ASSETS_KEY: "line1\nINJECTED=1" }, "GROVE_ASSETS_KEY"],
];
for (const [label, envs, key] of badCases) {
  const r = run({ apps: [hubApp(envs)] });
  check(`${label}: exits 1`, r.status === 1, `status ${r.status}`);
  check(`${label}: names the key in an ::error::`, r.stderr.includes("::error::") && r.stderr.includes(key));
  check(`${label}: writes nothing`, r.env === "EXISTING=1\n", JSON.stringify(r.env));
}
{
  const { GROVE_ASSETS_OPTIMIZE_TOKEN: _drop, ...missing } = GOOD;
  const r = run({ apps: [hubApp(missing)] });
  check("a missing key: exits 1 and writes nothing", r.status === 1 && r.env === "EXISTING=1\n");
}
{
  const r = run({ apps: [otherApp] });
  check("no hub app: exits 1 and writes nothing", r.status === 1 && r.env === "EXISTING=1\n");
  check("no hub app: says so", r.stderr.includes("grove-hub-prod"));
}

console.log("promote-storefronts.yml stays wired to the script");
{
  const wf = readFileSync(workflow, "utf8");
  check("workflow calls scripts/ci/hub-assets-env.sh", wf.includes("scripts/ci/hub-assets-env.sh"));
  const literal = wf
    .split("\n")
    .filter((l) => !l.trim().startsWith("#"))
    .filter((l) => /TF_VAR_grove_assets_(key|secret|optimize_token)=/.test(l));
  check(
    "workflow sets no grove_assets_* variable itself",
    literal.length === 0,
    literal.map((l) => l.trim()).join(" | "),
  );
  const callAt = wf.indexOf("scripts/ci/hub-assets-env.sh \"");
  const applyAt = wf.indexOf("- name: Terraform apply (targeted");
  check("the script runs before the targeted apply", callAt > 0 && applyAt > callAt);
}

if (failures > 0) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("\nAll checks passed.");
