# ADR 004: QA promotion model — per-repo `qa` branches + manifest + AgenticOS-routed failure triage

**Status:** Accepted (2026-06-25)
**Context:** Grove deployment pipeline across 3 repos (this one, `grove-sites`, `grove-odoo-modules`). Forced by the 2026-06-24/25 incident where QA pulled a week-old frontend `:latest` image while Josh's local OrbStack ran current code — multi-repo coordination broke down because nothing was the single source of truth for "what combo is in QA."

## Decision

Adopt a per-repo `qa` branch model where each upstream repo publishes immutable SHA-tagged images on every `qa` push, and `odoocker-goldberrygrove` holds a **release manifest** (`infra/release-manifest.yaml`) that pins which specific SHAs are deployed to the QA environment. Promotion to prod happens by merging `qa → main` in odoocker (which carries the validated manifest values to main).

Automated smoke tests gate promotion. Failures route to AgenticOS Dev Agent via a Paperclip routine webhook for `diagnose + draft PR` triage. Human reviews the draft PR and decides whether to merge.

## Context

Three repos contribute to any QA deploy:

| Repo | Produces | Deploy mechanism |
|---|---|---|
| `grove-sites` | 4 frontend Docker images (hub, goldberry, ggg, nursery) — matrix-built from one commit | GHCR pull via compose |
| `odoocker-goldberrygrove` | grove-odoo Docker image + the orchestration (TF env, compose, workflows) | GHCR pull via compose; TF apply for infra |
| `grove-odoo-modules` | Custom Odoo addons (no image — git ref consumed by git-sync) | git-sync at runtime |

Before this ADR, there was no synchronizing layer between them. `:latest` floated independently in each repo; the QA env pulled whatever `:latest` happened to point at when the deploy ran. The 2026-06-24/25 incident exposed the failure mode: a frontend image from `grove-sites` main last built on 2026-06-17 (because subsequent commits to grove-sites main only touched workflow files, which the Docker workflow's path filter excluded) was pulled by a QA deploy a week later; the QA env didn't match Josh's local OrbStack at all.

The deeper symptom: there was no single artifact that answered "what code is in QA right now?" and therefore no audit trail when QA found a bug.

## Alternatives considered

| Option | Why not |
|---|---|
| **Model A — per-repo `qa` branches + floating `:qa` image tag** | Race conditions when multiple repos move in the same window. No record of "which combo was tested together." The 2026-06-24 incident is the exact failure mode this still allows. |
| **Model C — per-PR preview environments for all 3 repos** | Over-engineering for solo-dev. `grove-sites` already has `preview-up.yml` for per-PR previews; layering C on top of B for grove-sites is a reasonable LATER addition. Building per-PR preview infra for the other 2 repos is months of work that doesn't address the multi-repo coordination problem. |
| **Webhook from upstream → odoocker auto-bump** | Too noisy. Every grove-sites qa push would auto-bump odoocker's manifest. Batched human-gated bumps (the chosen Option B for the bump trigger) preserve agency and let multiple upstream changes land in one QA cycle. |
| **Auto-merge after smoke pass** | Risk of false-negative smoke + silent prod break. The smoke test catches obvious breakage; subtle visual or UX bugs need human eyes. Auto-merge removes the eyes. |
| **Open `qa-broken` GH issue + `@claude` mention via Claude Code Action** | Works, but bypasses the existing AgenticOS / Paperclip platform (already operational, already has the Dev Agent role configured, already has the GitHub App with PR-write permissions). Using Paperclip's routine webhook keeps work-routing logic in the AgenticOS platform where the rest of the agent-orchestration lives. |
| **No automated failure triage; manual debug only** | The 2026-06-24 session showed how much time goes into "what does the failure log say, where do I SSH, what's the env state." Automating the first-pass triage saves the agent-equivalent cost on every QA failure; humans focus on the "do I accept the proposed fix" decision. |
| **Auto-fix without draft-PR gate** | One catastrophic auto-merge is worse than 100 manual reviews. Draft PR + human merge is the right safety boundary for agent-authored prod changes. |

## What this wins

- **Multi-repo deploy state is auditable.** The manifest file in odoocker's `qa` branch IS the answer to "what's in QA right now." Same file in `main` IS the answer to "what's in prod." Git history of the manifest IS the deploy timeline.
- **Promotion is intentional.** Three gates (manifest bump PR review, smoke test pass, manual qa→main PR with checklist) catch different failure modes.
- **Failure triage happens automatically without bypassing safety.** Dev Agent investigates, drafts a PR, posts findings. Human merges or rejects.
- **Bisecting QA failures is trivial.** Manifest git history shows every combo tested; binary search across manifest commits finds the regression-introducing upstream SHA.
- **Per-source granularity matches unit of intent.** When Josh says "test this grove-sites work," he means all 4 frontends from the same commit, not 4 separate decisions — the 3-entry manifest matches that.

## What this does not win

- **Doesn't reduce time-to-deploy.** Per-repo gates add steps. A QA cycle is ~5 actions (push to upstream qa, build completes, bump manifest, merge PR, deploy fires) instead of 1 (push to main).
- **Doesn't handle multiple concurrent QA cycles.** Single shared QA env today. If a second tester wants to validate a different combo while the first is still in flight, they have to wait or destroy. Per-PR preview envs (deferred) are the long-term answer.
- **Doesn't prevent upstream-repo image-build failures.** If `grove-sites:qa` push fails to build, the manifest can't pin a SHA from that commit — but neither could a floating `:qa` tag, so this isn't a regression.
- **Doesn't shorten the cross-repo coordination latency.** Three repos in the loop = three places to push, three build cycles. Inherent to the multi-repo split (decided in [ADR 001](./001-separate-modules-repo.md) and downstream).
- **Doesn't define a hotfix path.** Hotfix that bypasses QA is intentionally out of scope for v1 — will be designed when first incident demands it.

## Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│ UPSTREAM REPOS — each has a long-lived `qa` branch                     │
│                                                                        │
│   grove-sites:qa            push → matrix build → GHCR                 │
│       └─ grove-hub:sha-<commit>                                        │
│       └─ grove-goldberry:sha-<commit>                                  │
│       └─ grove-ggg:sha-<commit>                                        │
│       └─ grove-nursery:sha-<commit>                                    │
│       (NO floating :qa tag — manifest is the only thing that pins)    │
│                                                                        │
│   odoocker:qa (this repo)   push → docker-odoo build → GHCR           │
│       └─ grove-odoo:sha-<commit>                                       │
│                                                                        │
│   grove-odoo-modules:qa     push → (no build; git ref is the artifact)│
└───────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────────────┐
│ ODOOCKER (this repo) — orchestrates the QA combo                       │
│                                                                        │
│   scripts/bump-qa-manifest.sh                                          │
│     reads latest :sha-<commit> from each upstream qa branch            │
│     writes infra/release-manifest.yaml                                 │
│     opens PR to odoocker:qa                                            │
│                                                                        │
│   Human reviews the manifest diff PR + merges                          │
│                                                                        │
│   qa-deploy.yml fires on push to odoocker:qa                           │
│     reads release-manifest.yaml                                        │
│     templates SHAs into compose env vars                              │
│     TF apply → droplet + cloud-init → grove-ready sentinel             │
│                                                                        │
│   qa-smoke.yml runs against the deployed URLs                          │
└───────────────────────────────────────────────────────────────────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  ▼                               ▼
      [ smoke pass ✅ ]                  [ smoke fail 🚨 ]
                  │                               │
                  ▼                               ▼
   Discord post: "QA ready"     Discord post: "smoke failed"
                  │                               │
                  │                               ├─► GH issue opened
                  │                               │   (audit trail; label qa-broken)
                  │                               │
                  │                               └─► Paperclip routine webhook fired
                  │                                   (CF Access service token in headers,
                  │                                    HMAC-signed body)
                  │                                   │
                  │                                   ▼
                  │                            Paperclip creates run → issue
                  │                            assigned to Dev Agent
                  │                                   │
                  │                                   ▼
                  │                            Dev Agent heartbeat picks up
                  │                            brainstorm(Claude) → execute(Codex)
                  │                                   │
                  │                                   ▼
                  │                            Draft PR back to relevant repo
                  │                            via AgenticOS Developer App's
                  │                            installation token
                  │                                   │
                  ▼                                   ▼
   Human opens qa→main promotion PRs       Human reviews draft PR
   (per-repo, manual, with checklist)       marks ready + merges
   → main branches updated                  → next qa cycle iterates
   → main pushes trigger :latest publish
   → (future M4) prod deploy
```

## Branch semantics

| Branch | Purpose | Image tag produced | Deploy target |
|---|---|---|---|
| Any feature branch | Work in progress | None (per-PR build only, no publish) | grove-sites: per-PR preview droplet (existing). Others: no preview yet. |
| `qa` (each repo) | Code under QA validation | `:sha-<commit>` only — no floating tag | odoocker `qa` push triggers `qa-deploy.yml` |
| `main` (each repo) | Validated code | `:sha-<commit>` + `:latest` | odoocker `main` push triggers prod deploy (when M4 ships) |

The manifest pins by `:sha-<commit>` exclusively. `:latest` is preserved for backward compatibility with anything that pulls "latest" manually, but production deploys read SHAs from the manifest, not `:latest`.

## CF Access integration (the one real wrinkle)

Paperclip runs on the AgenticOS droplet behind Cloudflare Access (Google SSO). A GitHub Actions runner POSTing to a Paperclip routine webhook URL gets a 302 to `cloudflareaccess.com/login` instead of reaching Paperclip — not a network error, an Access gate.

**Solution:** a Cloudflare Access service token, terraformed alongside Paperclip's Access app:

```hcl
resource "cloudflare_zero_trust_access_service_token" "qa_triage" {
  account_id           = var.cloudflare_account_id
  name                 = "odoocker-qa-triage"
  min_days_for_renewal = 30
}

resource "cloudflare_zero_trust_access_policy" "qa_triage_svc" {
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.paperclip.id
  name           = "Allow odoocker-qa-triage service token"
  precedence     = 1
  decision       = "non_identity"
  include { service_token = [cloudflare_zero_trust_access_service_token.qa_triage.id] }
}
```

The workflow sends `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers alongside the HMAC-signed body. Cloudflare validates the token (gate pass); Paperclip validates HMAC (auth). Two independent gates, both pass = work created.

## Secrets inventory (Infisical `grove-odoocker/prod`)

| Secret | Purpose | Source |
|---|---|---|
| `PAPERCLIP_QA_TRIAGE_WEBHOOK_URL` | Routine's public webhook URL | Paperclip API response when routine trigger created |
| `PAPERCLIP_QA_TRIAGE_WEBHOOK_SECRET` | HMAC signing secret | Same response |
| `PAPERCLIP_QA_TRIAGE_CF_CLIENT_ID` | CF Access service token public ID | TF output of `cloudflare_zero_trust_access_service_token.qa_triage` |
| `PAPERCLIP_QA_TRIAGE_CF_CLIENT_SECRET` | CF Access service token secret | Same TF output |

## Sequencing dependencies

This design only fully lights up when these are all true:

1. **AgenticOS Developer App installed on `Goldberry-Playground` org with `odoocker-goldberrygrove` in selected repos.** Without this, Dev Agent's PR-back fails. Verified at design time: app exists (#4134853, per `wiki/Software/AgenticOS.md`), org install scope needs explicit confirmation per-repo.
2. **AgenticOS token-broker + Dev Agent GitHub-auth rollout complete** (AgenticOS Asana #204). Until this lands, Dev Agent surfaces findings as Paperclip-side comments only; PR-back doesn't happen.
3. **`agent-house-rules.md` updated to specify "open PRs as drafts; human marks ready."** Current rules say "always branch + PR" but don't constrain draft vs ready. The draft-PR contract is the safety boundary for this design; it must be explicit.
4. **CF Access service token provisioned + 4 secrets seeded in Infisical `grove-odoocker/prod`.**

Until all 4 are true, the smoke-failure path partially degrades: GH audit issue still opens (always works), Paperclip routine fires (works once CF access token is in place), but agent action stops at "post findings" instead of "draft PR" (requires #1 + #2 + #3).

## Implementation phases

| Phase | Deliverable | Repo(s) |
|---|---|---|
| 1 | Per-repo `qa` branch created + branch protection (PR required, no force push) | all 3 |
| 2 | Image build workflow modified to publish `:sha-<commit>` on `qa` push | grove-sites, odoocker |
| 3 | `infra/release-manifest.yaml` + parser in `qa-deploy.yml` | odoocker |
| 4 | `scripts/bump-qa-manifest.sh` + `make bump-qa-manifest` target | odoocker |
| 5 | `qa-smoke.yml` workflow with HTTP probe + content check + Discord post | odoocker |
| 6 | CF Access service token TF + secret seeding script | AgenticOS-side TF env |
| 7 | Paperclip routine created (via dashboard or API), webhook URL + secret seeded | manual / AgenticOS-side |
| 8 | Smoke-failure step in `qa-smoke.yml` fires routine webhook + opens GH issue | odoocker |
| 9 | `agent-house-rules.md` line added: "Open PRs as drafts; human marks ready" | AgenticOS-side |
| 10 | End-to-end test: induce a smoke failure, verify Dev Agent opens a draft PR | all |

Phases 1-5 land first as `odoocker`-internal work; 6-10 require AgenticOS-side changes + can land in parallel.

## Open follow-ups

- **GH-issues sync routine (AgenticOS Asana Step 9) collision.** When that ships, the `qa-broken` audit issues would also get synced + routed unless the sync skips that label. Either the sync excludes `qa-broken` OR the audit issue uses a different label and the sync ignores it. Decide when Step 9 lands.
- **Per-PR previews for odoocker + grove-odoo-modules.** Pain-driven add. `grove-sites` already has previews; the others don't. Layer C on top of B when warranted.
- **Hotfix path that bypasses QA.** Not designed yet. Can be deferred until first prod incident.
- **Concurrent QA cycles.** Single shared QA env today. If/when a second human tester needs to validate a different combo simultaneously, designs needed (per-PR previews are the natural answer).
- **`make promote-qa` target (Option β).** Opens all 3 promotion PRs in one shot. Skip until manual α gets tedious.
- **Full auto-merge for narrowly-scoped fix patterns.** "If smoke failure is HTTP 502 on tenant URL and agent's diagnosis matches port-mismatch signature, allow auto-merge." Defer until trust + pattern library exists.
- **Production deploy pipeline (M4).** This ADR defines the qa→main shape but not what main pushes do. The promotion mechanic is ready; the prod-side workflows are deferred to M4.
