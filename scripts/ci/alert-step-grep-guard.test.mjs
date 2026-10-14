#!/usr/bin/env node
// Regression test for the GREP-KILLS-THE-ALERT defect class (GOL-2630).
//
// What it protects:
//   Every Grove watcher builds its Discord embed body the same way --
//   `DETAIL=$(grep ... file | head ...)` -- and then calls discord-status.sh.
//   A no-match grep exits 1. Whether that kills the step depends entirely on
//   which shell GitHub Actions picked:
//
//     no `shell:` key  -> bash -e {0}                      (NO pipefail)
//     shell: bash      -> bash --noprofile --norc -eo pipefail {0}
//
//   Under the default, the pipeline reports `head`'s 0 and grep's 1 is
//   swallowed -- the alert survives BY ACCIDENT. Add `shell: bash` for an
//   unrelated reason, or drop the trailing `| head`, and `set -e` kills the
//   step BEFORE discord-status.sh runs. The alert that exists to be loud goes
//   missing exactly on the paths that produce no matching lines: for
//   obs-firewall-drift.yml that is every exit-2 "watcher is broken" path (no
//   DO token, unparseable variables.tf, DO API unreachable, no such firewall),
//   because check-firewall.sh prints its first `firewall:` line only after the
//   env checks pass. Same silently-dead-alert class as GOL-2564's `--branch`
//   bug: nobody notices, because a watcher that says nothing looks exactly
//   like a watcher with nothing to say.
//
//   So this file asserts both halves:
//     1. STATIC, repo-wide -- every grep in a step that calls
//        discord-status.sh is `|| true`-guarded, so the guard holds for
//        watchers written after this one.
//     2. BEHAVIORAL -- obs-firewall-drift.yml's Discord step still posts under
//        the STRICTER shell (-eo pipefail) with an fw.out that has no matching
//        line, plus a negative control proving this test actually fails when
//        the `|| true` is removed.
//
// node builtins only -- run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, chmodSync, readFileSync, readdirSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
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

// ── Minimal `run: |` block extractor ──────────────────────────────────────
// Deliberately NOT a YAML parse: `scripts/` is not a Node workspace and the
// CI scripts job installs nothing, so there is no js-yaml to import. Block
// scalars are the only shape we need, and their indentation rule is simple.
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
      if (lines[j].trim() === "") {
        body.push("");
        continue;
      }
      if (lines[j].match(/^(\s*)/)[1].length <= indent) break;
      body.push(lines[j]);
    }
    const widths = body.filter((l) => l.trim() !== "").map((l) => l.match(/^(\s*)/)[1].length);
    const dedent = widths.length ? Math.min(...widths) : 0;
    blocks.push({ step: stepName, body: body.map((l) => l.slice(dedent)).join("\n") });
    i = j - 1;
  }
  return blocks;
};

// ── 1. Static: no unguarded grep in any alerting step ─────────────────────
// A grep is "guarded" if its failure cannot abort the step: either it is
// already the condition of an if/elif/while/until (where `set -e` is
// suspended by definition), or the line ends the failure with `|| true` /
// `|| :`. Comment lines are not code.
const isGuarded = (line) => {
  const code = line.trim();
  if (code.startsWith("#")) return true;
  if (/^(if|elif|while|until)\b/.test(code)) return true;
  if (/\|\|\s*(true|:)\s*\)?\s*$/.test(code)) return true;
  return false;
};

let alertingSteps = 0;
for (const file of readdirSync(workflowDir).filter((f) => f.endsWith(".yml") || f.endsWith(".yaml"))) {
  const text = readFileSync(join(workflowDir, file), "utf8");
  for (const { step, body } of extractRunBlocks(text)) {
    if (!body.includes("discord-status.sh")) continue;
    alertingSteps++;
    const offenders = body
      .split("\n")
      .filter((l) => /\bgrep\b/.test(l) && !isGuarded(l))
      .map((l) => l.trim());
    check(
      `${file} :: ${step ?? "(unnamed step)"} -- every grep is \`|| true\`-guarded`,
      offenders.length === 0,
      offenders.length
        ? `a no-match grep exits 1 and would abort this step before discord-status.sh runs:\n       ${offenders.join("\n       ")}`
        : "",
    );
  }
}
// Guard the guard: if the extractor silently stops matching (an indentation
// or `run:` style change), the loop above goes vacuously green.
check(
  "found the alerting steps to check (>= 2)",
  alertingSteps >= 2,
  `only ${alertingSteps} step(s) calling discord-status.sh were extracted — the run-block extractor is probably broken`,
);

// ── 2. Behavioral: the obs drift alert posts on a no-match fw.out ─────────
const obsText = readFileSync(join(workflowDir, "obs-firewall-drift.yml"), "utf8");
const discordStep = extractRunBlocks(obsText).find(
  (b) => (b.step ?? "").startsWith("Discord") && b.body.includes("fw.out"),
);
check("obs-firewall-drift.yml :: Discord step body extracted", Boolean(discordStep));

if (discordStep) {
  const dir = mkdtempSync(join(tmpdir(), "alert-step-grep-"));
  mkdirSync(join(dir, "scripts"), { recursive: true });

  // Stub stands in for the real alerting path: records the call, never posts.
  const stub = join(dir, "scripts", "discord-status.sh");
  writeFileSync(stub, `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> "$DISCORD_CALLS"\n`);
  chmodSync(stub, 0o755);

  writeFileSync(join(dir, "step.sh"), discordStep.body);
  // No-match fixture: exactly what fw.out looks like on every exit-2 path,
  // because check-firewall.sh dies before printing its first `firewall:` line.
  writeFileSync(join(dir, "fw-nomatch.out"), "ERROR: DIGITALOCEAN_TOKEN is not set\n");
  writeFileSync(
    join(dir, "fw-drift.out"),
    "firewall: grove-obs-fw (f00)\n  DRIFT inbound 22: missing 173.84.140.152/32\nnote: tag env-qa-l3 matches 0 droplets\n",
  );

  const runStep = ({ shellArgs, rc, fixture, script = "step.sh" }) => {
    const callLog = join(dir, `calls-${Math.random().toString(36).slice(2)}`);
    spawnSync("cp", [join(dir, fixture), join(dir, "fw.out")]);
    const res = spawnSync("bash", [...shellArgs, script], {
      cwd: dir,
      encoding: "utf8",
      env: {
        PATH: process.env.PATH,
        HOME: dir,
        DISCORD_CALLS: callLog,
        RC: String(rc),
        OBS_DIR: "infra/terraform/environments/observability",
        RUN_URL: "https://example.invalid/run/1",
        // Never inherit the live ops webhook into a test child.
        DISCORD_OPS_WEBHOOK_URL: "",
      },
    });
    return {
      status: res.status,
      stderr: res.stderr,
      posted: existsSync(callLog) ? readFileSync(callLog, "utf8") : "",
    };
  };

  // The stricter shell is the point: it is what `shell: bash` would give us,
  // and it must not change the outcome.
  for (const [label, shellArgs] of [
    ["bash -e (Actions default)", ["-e"]],
    ["bash -eo pipefail (shell: bash)", ["-eo", "pipefail"]],
  ]) {
    const broken = runStep({ shellArgs, rc: 2, fixture: "fw-nomatch.out" });
    check(
      `${label} :: rc=2 + no matching line -> "check BROKEN" alert still posts`,
      broken.status === 0 && broken.posted.includes("check BROKEN"),
      `exit=${broken.status} posted=${JSON.stringify(broken.posted)} stderr=${broken.stderr.trim()}`,
    );

    const drift = runStep({ shellArgs, rc: 1, fixture: "fw-drift.out" });
    check(
      `${label} :: rc=1 -> DRIFT alert posts with the DRIFT lines`,
      drift.status === 0 && drift.posted.includes("DRIFT") && drift.posted.includes("173.84.140.152/32"),
      `exit=${drift.status} posted=${JSON.stringify(drift.posted)} stderr=${drift.stderr.trim()}`,
    );
  }

  // ── Negative control ───────────────────────────────────────────────────
  // Strip the guard and the no-match path MUST go silent under pipefail. If
  // this assertion fails, the two above are not actually testing anything.
  const unguarded = discordStep.body.replace(/\s*\|\|\s*true\s*\)/g, ")");
  check(
    "negative control :: the `|| true` was found and removable",
    unguarded !== discordStep.body,
    "no `|| true)` in the extracted step -- the guard this test protects is gone or reshaped",
  );
  writeFileSync(join(dir, "unguarded.sh"), unguarded);
  const control = runStep({
    shellArgs: ["-eo", "pipefail"],
    rc: 2,
    fixture: "fw-nomatch.out",
    script: "unguarded.sh",
  });
  check(
    "negative control :: without `|| true`, pipefail kills the step and NOTHING posts",
    control.status !== 0 && control.posted === "",
    `exit=${control.status} posted=${JSON.stringify(control.posted)} -- expected a dead step, so this test can detect the regression`,
  );
}

console.log(failures === 0 ? "\nalert-step-grep-guard: PASS" : `\nalert-step-grep-guard: ${failures} failure(s)`);
process.exit(failures === 0 ? 0 : 1);
