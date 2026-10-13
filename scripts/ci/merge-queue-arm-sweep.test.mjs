#!/usr/bin/env node
// Behavioral test for scripts/ci/merge-queue-arm-sweep.sh (GOL-3159).
//
// What it protects:
//   This wrapper is the thing that will run unattended forever, so its job is
//   less "arm correctly" (merge-queue-arm-automerge.test.mjs covers that) than
//   "keep running, and tell the truth about whether it is working".
//
//   Three properties are load-bearing, and all three are easy to break by a
//   well-meaning edit:
//
//     1. ONE REPO'S FAILURE MUST NOT SKIP THE OTHERS. A broker scope problem on
//        one repo is precisely when the other two still need sweeping. A loop
//        that lets the inner script's exit status escape silently turns a
//        one-repo outage into a three-repo outage -- and nothing looks wrong
//        except that PRs stop merging.
//
//        Note for whoever edits the wrapper: adding `set -e` to it does NOT
//        break this, and the mutation test for it was initially wrong for that
//        reason. `out="$(...)" || rc=$?` is already an `||` context, so `set -e`
//        does not fire there. What actually breaks it is dropping that rc
//        capture (or `exit`ing from inside the loop) -- both of those were
//        verified to turn these three checks red.
//
//     2. HARD vs SOFT must stay distinguished. GitHub refusing one individual
//        arm (stale head, queue race) heals itself on the next cadence, because
//        the inner script is idempotent. The broker being unreachable does not.
//        If a soft failure paged, the ops channel would fill with self-healing
//        noise and then be muted -- which is the same outcome as no alerting.
//
//     3. THE STREAK MUST NOT PAGE ON THE FIRST FAILURE, AND MUST NOT PAGE
//        FOREVER. Threshold crossing fires once; a permanently broken repo
//        re-posts only every SWEEP_FAIL_REPEAT runs.
//
//   The wrapper is driven as a black box with a STUB inner script, because the
//   contract under test is the wrapper's reading of the inner script's output
//   (`FATAL:`, the ` repo=<name> ` banner, `armed #N`) -- not the inner logic.
//   Stubbing the real `merge-queue-arm-automerge.sh` also keeps the test
//   hermetic: no broker, no token, no network, no GitHub.
//
// node builtins only -- run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, chmodSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const sweep = join(repoRoot, "scripts", "ci", "merge-queue-arm-sweep.sh");

if (!existsSync(sweep)) {
  console.error(`FAIL: ${sweep} does not exist.`);
  process.exit(1);
}

let failures = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures++;
    console.error(`  FAIL ${name}${detail ? ` -- ${detail}` : ""}`);
  }
};

const A = "Goldberry-Playground/odoocker-goldberrygrove";
const B = "Goldberry-Playground/grove-odoo-modules";
const C = "Goldberry-Playground/grove-sites";

// Stub inner script. Behaviour per repo comes from a BEHAVIOUR env var the stub
// parses, so one stub covers every case.
//
//   ok:N     evaluated cleanly, armed N PRs, exit 0
//   soft     evaluated, refused one arm, exit 1   (self-healing)
//   fatal    printed FATAL:, exit 1               (could not evaluate)
//   crash    printed nothing at all, exit 127     (no banner => hard)
const STUB = `#!/usr/bin/env bash
set -uo pipefail
mode=""
for spec in $BEHAVIOUR; do
  case "$spec" in
    "$REPO="*) mode="\${spec#*=}" ;;
  esac
done
echo "$REPO" >> "$CALLED_LOG"
case "$mode" in
  crash) exit 127 ;;
  fatal)
    echo "00:00:00Z FATAL: broker key not readable at /paperclip/gh-broker.key"
    exit 1 ;;
esac
echo "00:00:00Z repo=$REPO branch=main app=agenticos-developer arm_unapproved=\${ARM_UNAPPROVED:-} arm_protected=\${ARM_PROTECTED:-0} apply=$# "
if [ "$mode" = "soft" ]; then
  echo "00:00:00Z WARN could not arm #42: Pull request is in unexpected state"
  exit 1
fi
n="\${mode#ok:}"
[ "$n" = "$mode" ] && n=0
i=0
while [ "$i" -lt "$n" ]; do
  i=$((i + 1))
  echo "00:00:00Z armed #\${i}0 (head abcdef012) as agenticos-developer"
done
exit 0
`;

function run({ behaviour, repos = [A, B, C], stateDir, apply = false, env = {}, threshold, repeat }) {
  const dir = mkdtempSync(join(tmpdir(), "arm-sweep-test-"));
  const stub = join(dir, "arm.sh");
  writeFileSync(stub, STUB);
  chmodSync(stub, 0o755);
  const calledLog = join(dir, "called.log");
  writeFileSync(calledLog, "");

  const r = spawnSync("bash", [sweep, ...(apply ? ["--apply"] : [])], {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH,
      HOME: dir,
      TMPDIR: dir,
      SWEEP_ARM_SCRIPT: stub,
      SWEEP_REPOS: repos.join(" "),
      SWEEP_STATE_DIR: stateDir ?? join(dir, "state"),
      BEHAVIOUR: behaviour,
      CALLED_LOG: calledLog,
      ...(threshold !== undefined ? { SWEEP_FAIL_THRESHOLD: String(threshold) } : {}),
      ...(repeat !== undefined ? { SWEEP_FAIL_REPEAT: String(repeat) } : {}),
      // DISCORD_OPS_WEBHOOK_URL is deliberately absent: it is live in the agent
      // environment and would post for real. The wrapper logs the alert text
      // instead when it is unset, which is what these tests assert on.
      ...env,
    },
  });
  return {
    ...r,
    out: `${r.stdout || ""}${r.stderr || ""}`,
    called: readFileSync(calledLog, "utf8").trim().split("\n").filter(Boolean),
    dir,
  };
}

console.log("merge-queue-arm-sweep.sh");

// ── 1. all three repos are swept, and ARM_UNAPPROVED=1 is forced ────────────
{
  const r = run({ behaviour: `${A}=ok:1 ${B}=ok:0 ${C}=ok:2` });
  check("sweeps every repo in SWEEP_REPOS", r.called.length === 3 && r.called.includes(A) && r.called.includes(C),
    `called=${JSON.stringify(r.called)}`);
  check("exits 0 when every repo evaluates", r.status === 0, `status=${r.status}`);
  check("forces ARM_UNAPPROVED=1 (the only mode that beats approval)",
    /arm_unapproved=1/.test(r.out));
  check("never sets ARM_PROTECTED (board-gated)", /arm_protected=0/.test(r.out) && !/arm_protected=1/.test(r.out));
  check("counts armed PRs across repos", /armed=3\b/.test(r.out), r.out);
}

// ── 2. dry run by default; --apply is passed through ────────────────────────
{
  const dry = run({ behaviour: `${A}=ok:0`, repos: [A] });
  check("defaults to dry run (no --apply forwarded)", /apply=0\b/.test(dry.out), dry.out);
  const live = run({ behaviour: `${A}=ok:0`, repos: [A], apply: true });
  check("forwards --apply to the arming script", /apply=1\b/.test(live.out), live.out);
}

// ── 3. one repo's HARD failure must not skip the others ─────────────────────
{
  const r = run({ behaviour: `${A}=fatal ${B}=ok:1 ${C}=ok:1` });
  check("a FATAL on repo 1 still sweeps repos 2 and 3",
    r.called.length === 3, `called=${JSON.stringify(r.called)}`);
  check("still arms on the healthy repos", /armed=2\b/.test(r.out), r.out);
  check("exits non-zero when a repo could not be evaluated", r.status !== 0, `status=${r.status}`);
  check("names the hard-failed repo", /hard_failed=\[Goldberry-Playground\/odoocker-goldberrygrove\]/.test(r.out), r.out);
}

// ── 4. a stub that prints nothing is hard, not silently fine ────────────────
{
  const r = run({ behaviour: `${A}=crash`, repos: [A] });
  check("no evaluation banner is treated as a HARD failure",
    r.status !== 0 && /HARD failure/.test(r.out), r.out);
}

// ── 5. SOFT failure: counted, logged, NOT fatal, NOT paged ──────────────────
{
  const r = run({ behaviour: `${A}=soft`, repos: [A], threshold: 1 });
  check("a refused individual arm exits 0 (it retries next cadence)",
    r.status === 0, `status=${r.status}`);
  check("a refused arm is reported as soft", /soft failure/.test(r.out) && /soft_failed=\[.*odoocker/.test(r.out), r.out);
  check("a soft failure never produces an alert even at threshold 1",
    !/alert not posted/.test(r.out) && !/arm sweep failing/.test(r.out), r.out);
}

// ── 6. streak: no page on the first failure, one page at the threshold ──────
{
  const dir = mkdtempSync(join(tmpdir(), "arm-sweep-streak-"));
  const state = join(dir, "state");
  const go = () => run({ behaviour: `${A}=fatal`, repos: [A], stateDir: state, threshold: 3, repeat: 12 });

  const r1 = go();
  check("1st consecutive hard failure does not alert", !/arm sweep failing/.test(r1.out), r1.out);
  const r2 = go();
  check("2nd consecutive hard failure does not alert", !/arm sweep failing/.test(r2.out), r2.out);
  const r3 = go();
  check("3rd consecutive hard failure alerts (threshold crossing)",
    /arm sweep failing/.test(r3.out) && /3 consecutive hard failures/.test(r3.out), r3.out);
  const r4 = go();
  check("4th does NOT re-alert (no per-cadence spam)", !/arm sweep failing/.test(r4.out), r4.out);

  const persisted = JSON.parse(readFileSync(join(state, "fail-streaks.json"), "utf8"));
  check("streak is persisted per repo", persisted[A] === 4, JSON.stringify(persisted));

  // Recovery resets the streak and says so exactly once.
  const rOk = run({ behaviour: `${A}=ok:1`, repos: [A], stateDir: state, threshold: 3 });
  check("recovery alerts once", /arm sweep recovered/.test(rOk.out), rOk.out);
  const rOk2 = run({ behaviour: `${A}=ok:1`, repos: [A], stateDir: state, threshold: 3 });
  check("recovery does not re-alert", !/arm sweep recovered/.test(rOk2.out), rOk2.out);
  check("streak reset to 0 after recovery",
    JSON.parse(readFileSync(join(state, "fail-streaks.json"), "utf8"))[A] === 0);
}

// ── 7. repeat window re-alerts for a persistently broken repo ───────────────
{
  const dir = mkdtempSync(join(tmpdir(), "arm-sweep-repeat-"));
  const state = join(dir, "state");
  const go = () => run({ behaviour: `${A}=fatal`, repos: [A], stateDir: state, threshold: 1, repeat: 3 });
  const alerted = [];
  for (let i = 1; i <= 7; i++) alerted.push(/arm sweep failing/.test(go().out));
  // threshold 1 -> alert at n=1, then every 3rd after: n=4, n=7.
  check("re-alerts on the repeat window only",
    JSON.stringify(alerted) === JSON.stringify([true, false, false, true, false, false, true]),
    JSON.stringify(alerted));
}

// ── 8. a corrupt state file must not stop the sweep ─────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), "arm-sweep-corrupt-"));
  const state = join(dir, "state");
  const seed = run({ behaviour: `${A}=ok:1`, repos: [A], stateDir: state });
  check("state dir is created", existsSync(join(state, "fail-streaks.json")), seed.out);
  writeFileSync(join(state, "fail-streaks.json"), "{ this is not json");
  const r = run({ behaviour: `${A}=ok:1`, repos: [A], stateDir: state });
  check("corrupt state does not stop arming", r.status === 0 && /armed=1\b/.test(r.out), r.out);
}

// ── 9. an unwritable state dir degrades, it does not abort ──────────────────
{
  const r = run({ behaviour: `${A}=ok:1`, repos: [A], stateDir: "/proc/definitely-not-writable/x" });
  check("unwritable state dir falls back and keeps arming",
    r.status === 0 && /armed=1\b/.test(r.out) && /state dir .* unusable/.test(r.out), r.out);
}

// ── 10. a missing arming script fails loudly rather than reporting success ──
{
  const r = spawnSync("bash", [sweep], {
    encoding: "utf8",
    env: { PATH: process.env.PATH, SWEEP_ARM_SCRIPT: "/nonexistent/arm.sh", SWEEP_REPOS: A },
  });
  check("missing arming script is FATAL",
    r.status !== 0 && /FATAL: arming script not readable/.test(`${r.stdout}${r.stderr}`),
    `${r.status}: ${r.stdout}${r.stderr}`);
}

console.log(failures === 0 ? "\nAll merge-queue-arm-sweep checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
