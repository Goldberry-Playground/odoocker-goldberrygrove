#!/usr/bin/env node
// consult-target.mjs — verify the AgriforestryOS build that
// promote-storefronts.yml's optional `consult_tag` input asks to pin on
// grove-consult-prod (GOL-3723).
//
// The consult image is built by AgriforestryOS (consult-image.yml), not
// grove-sites, so its pin (`consult_image_tag`) is a different repo's SHA than
// the four storefronts'. There is no auto-resolve here on purpose: a blank
// `consult_tag` means "leave the consult pin alone", never "take the newest".
//
// What this checks, all BEFORE the approval gate (same reason as
// storefront-target.mjs: never ask a human to approve a SHA with no image):
//   1. blank input -> nothing to do; `consult_tag=` (empty) is written.
//   2. the input is a 40-char lowercase hex SHA.
//   3. the SHA is a commit ON AgriforestryOS main. This is what catches a
//      grove-sites SHA pasted into the wrong box: `grove-consult` still holds
//      tags published by grove-sites' retired consult leg, so the image gate
//      alone would wave one through and pin prod to a superseded build.
//   4. ghcr.io/goldberry-playground/grove-consult:<sha> exists.
//   5. a target behind the committed pin is a loud WARNING, not an error: the
//      input is always explicit, so it is a deliberate rollback (the same rule
//      storefront-target.mjs applies to an explicit target_sha).
//
// CLI (node builtins only; Node >= 18 for global fetch):
//   env INPUT_CONSULT_TAG   optional 40-hex AgriforestryOS SHA
//   env GH_TOKEN            token for api.github.com (AgriforestryOS is
//                           public; unset works at the anonymous rate limit)
//   env GITHUB_OUTPUT       if set, `consult_tag=<sha or empty>` is appended
//   env GITHUB_STEP_SUMMARY if set, a markdown report is appended
//   env PROD_TF_VARIABLES   path to the prod variables.tf (default below)
//   exit 0 -> blank, or verified;  exit 1 -> anything else
import { appendFileSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { GHCR_OWNER, PROD_TF_VARIABLES, isFullSha, makeGhcr, parsePinnedShas } from "./storefront-target.mjs";

export const CONSULT_REPO = "Goldberry-Playground/AgriforestryOS";
export const CONSULT_IMAGE = "grove-consult";
export const CONSULT_PIN_VARIABLE = "consult_image_tag";

export function makeConsultGithub({ fetchImpl = fetch, token = "" } = {}) {
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return {
    // { status, aheadBy, behindBy } of `head` relative to `base`, or null when
    // GitHub does not know one of the two commits in this repo (404).
    compare: async (base, head) => {
      const res = await fetchImpl(`https://api.github.com/repos/${CONSULT_REPO}/compare/${base}...${head}`, { headers });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`GET compare/${base}...${head} -> HTTP ${res.status}`);
      const c = await res.json();
      return { status: c.status, aheadBy: c.ahead_by, behindBy: c.behind_by };
    },
  };
}

export async function verifyConsultTarget({ inputTag, github, ghcr, pinnedSha = "" }) {
  const sha = (inputTag || "").trim();
  const errors = [];
  const warnings = [];
  if (!sha) return { ok: true, sha: "", skipped: true, pinnedSha, errors, warnings };
  if (!isFullSha(sha)) {
    errors.push(`consult_tag '${sha}' is not a 40-char lowercase hex commit SHA.`);
    return { ok: false, sha, pinnedSha, errors, warnings };
  }

  // `main` relative to the target: ahead/identical <=> the target is on main.
  const vsMain = await github.compare(sha, "main");
  if (!vsMain) {
    errors.push(
      `${sha.slice(0, 8)} is not a commit in ${CONSULT_REPO}. consult_tag takes an AgriforestryOS SHA -- ` +
        `the grove-sites SHA in target_sha is a different repo's and does not belong here.`
    );
  } else if (vsMain.status !== "ahead" && vsMain.status !== "identical") {
    errors.push(`${sha.slice(0, 8)} is not on ${CONSULT_REPO} main (main is '${vsMain.status}' relative to it). Only main builds are promoted.`);
  }

  const imageStatus = await ghcr.manifestStatus(CONSULT_IMAGE, sha);
  if (imageStatus !== 200) {
    errors.push(
      imageStatus === 404
        ? `ghcr.io/${GHCR_OWNER}/${CONSULT_IMAGE}:${sha} NOT FOUND (404) -- AgriforestryOS consult-image.yml never published this SHA (it only runs on main pushes that touch apps/consult, or its run failed).`
        : `ghcr.io/${GHCR_OWNER}/${CONSULT_IMAGE}:${sha} returned HTTP ${imageStatus} (expected 200).`
    );
  }

  if (errors.length === 0 && pinnedSha && pinnedSha !== sha) {
    const vsPin = await github.compare(pinnedSha, sha);
    if (vsPin && (vsPin.status === "behind" || vsPin.status === "diverged")) {
      warnings.push(
        `consult target ${sha.slice(0, 8)} is ${vsPin.status === "behind" ? `${vsPin.behindBy} commit(s) BEHIND` : "NOT on the lineage of"} ` +
          `the committed consult pin ${pinnedSha.slice(0, 8)} -- this rolls grove-consult-prod BACKWARD. Confirm that is what you want before approving.`
      );
    }
  }
  return { ok: errors.length === 0, sha, imageStatus, pinnedSha, errors, warnings };
}

export function renderConsultSummary(r) {
  const out = ["## Consult build", ""];
  if (r.skipped) {
    out.push(`\`consult_tag\` was blank -- the consult pin is **not moved** by this run (committed pin: \`${r.pinnedSha || "unknown"}\`).`);
  } else {
    out.push("| | AgriforestryOS SHA |", "|---|---|", `| Currently pinned | \`${r.pinnedSha || "unknown"}\` |`, `| Proposed | \`${r.sha}\` |`, "");
    if (r.imageStatus !== undefined) {
      out.push(`\`ghcr.io/${GHCR_OWNER}/${CONSULT_IMAGE}:${r.sha.slice(0, 8)}…\` present: ${r.imageStatus === 200 ? "yes (200)" : `**NO (${r.imageStatus})**`}`);
    }
    if (r.ok && r.pinnedSha && r.pinnedSha !== r.sha) {
      out.push("", `Compare: https://github.com/${CONSULT_REPO}/compare/${r.pinnedSha}...${r.sha}`);
    }
  }
  out.push("", "The pin only reaches a running app while `consult_enabled` is true; with the gate off the consult target in the apply matches nothing.");
  for (const w of r.warnings) out.push("", `> [!WARNING]\n> ${w}`);
  for (const e of r.errors) out.push("", `> [!CAUTION]\n> ${e}`);
  out.push("");
  return out.join("\n");
}

// ── CLI ─────────────────────────────────────────────────────────────────────
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [pinnedSha] = parsePinnedShas(readFileSync(process.env.PROD_TF_VARIABLES || PROD_TF_VARIABLES, "utf8"), [CONSULT_PIN_VARIABLE]);
    const r = await verifyConsultTarget({
      inputTag: process.env.INPUT_CONSULT_TAG,
      github: makeConsultGithub({ token: process.env.GH_TOKEN || process.env.GITHUB_TOKEN || "" }),
      ghcr: makeGhcr(),
      pinnedSha,
    });
    const summary = renderConsultSummary(r);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary + "\n");
    else process.stdout.write(summary + "\n");
    for (const w of r.warnings) console.log(`::warning::${w}`);
    for (const e of r.errors) console.log(`::error::${e}`);
    if (!r.ok) process.exit(1);
    if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `consult_tag=${r.sha}\n`);
    console.log(r.skipped ? `consult_tag blank; consult pin stays at ${pinnedSha}.` : `Verified consult_tag=${r.sha}; image present.`);
  } catch (e) {
    console.log(`::error::consult target verification failed: ${e.message}`);
    process.exit(1);
  }
}
