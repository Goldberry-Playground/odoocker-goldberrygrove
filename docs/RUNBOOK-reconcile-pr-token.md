# RUNBOOK — provision `RECONCILE_PR_TOKEN` (GOL-2504)

**Owner of this step: Josh / CEO.** An agent cannot do it — the ops 1Password
service account has no permission to write GitHub Actions secrets, and minting a
PAT/App is an access-control change.

**Status until it is done:** the workflow change is a *no-op*. Both call sites
use `${{ secrets.RECONCILE_PR_TOKEN || github.token }}`, so an absent secret
degrades to exactly today's behaviour.

## Why

`promote-storefronts.yml` and `reconcile-modules-pin.yml` both push a branch and
run `gh pr create`. Under the default `GITHUB_TOKEN` the PR author is
`github-actions[bot]`, and GitHub's anti-recursion guard suppresses every
`on: pull_request` workflow for GITHUB_TOKEN-originated events. So **CI**,
**Production Plan Guard**, **Security Scans**, **Infracost** and **CI autofix**
are created with conclusion `action_required` and never run.

The four required contexts —

- `Validate Docker Compose (Grove)`
- `Validate Nginx Config`
- `Lint Python (Odoo Modules)`
- `prod-plan-guard`

— therefore have **no check-run of any state**. The PR wedges at
"Expected — waiting for status" and the GOL-1958 missing-required-check sweep
files a fresh issue. Every promote/reconcile PR pays this tax; today's
remediation is a human pushing one empty commit under a non-bot identity (done
for #720 at `972c0b7e`, after which all four contexts went green).

Same class as GOL-2114 / GOL-2113 in `grove-odoo-modules`, fixed there by PR
#182 with this same fallback pattern.

## 🔴 Hard constraint on the identity

The token must **NOT** belong to `agenticos-developer` and **NOT** to
`EngineeringMoonBear`.

`.github/workflows/auto-approve.yml` classifies by PR author: those two logins
get auto-approve + auto-merge. These PRs bump the **production** storefront and
modules pins — they must stay human-gated (Josh / CEO). Any other identity falls
through auto-approve's `skipping (human/external PRs keep human review)` branch,
which is what we want: the PR still sits at `REVIEW_REQUIRED`.

## Provisioning recipe

Either option works; the fine-grained PAT is fewer moving parts.

### Option A — fine-grained PAT on a dedicated bot account (recommended)

1. Create (or reuse) a GitHub account that is **not** `EngineeringMoonBear` and
   is **not** the `agenticos-developer` App — e.g. `grove-reconcile-bot`.
2. Invite it to `Goldberry-Playground/odoocker-goldberrygrove` with **Write**
   access. It needs no other repo.
3. As that account: Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token.
   - Resource owner: `Goldberry-Playground`
   - Repository access: **Only select repositories** → `odoocker-goldberrygrove`
   - Repository permissions: **Contents: Read and write**,
     **Pull requests: Read and write**. Nothing else.
   - Expiration: set a real one (90 days) and calendar the rotation.
4. Store it in 1Password (`Grove Prod` vault) so rotation has a source of truth.
5. Repo → Settings → Secrets and variables → Actions → New repository secret:
   - Name: `RECONCILE_PR_TOKEN`
   - Value: the token

### Option B — a separate GitHub App

Same scopes (Contents: write, Pull requests: write), installed only on
`odoocker-goldberrygrove`, with a **different slug** from `agenticos-developer`.
This needs an extra `actions/create-github-app-token` step in both workflows to
exchange the App id + private key for an installation token — more wiring, but
short-lived tokens and no account seat. Only take this route if PAT expiry
churn becomes the problem.

## Verify (acceptance)

Next promote or reconcile run, on the PR it opens:

```bash
gh pr view <PR> --repo Goldberry-Playground/odoocker-goldberrygrove \
  --json author,reviewDecision -q '{author: .author.login, decision: .reviewDecision}'

gh pr checks <PR> --repo Goldberry-Playground/odoocker-goldberrygrove
```

Expected:

- `author` is the new bot — **not** `github-actions`, **not**
  `agenticos-developer`, **not** `EngineeringMoonBear`.
- All four required contexts appear as `pending`/`pass` — **not**
  `action_required` — with **no** manual empty commit.
- `reviewDecision` is `REVIEW_REQUIRED` (human gate preserved).

## Rotation / revocation

- Revoking or letting the PAT expire is **safe**: the `||` fallback silently
  returns to GITHUB_TOKEN behaviour. Nothing breaks; the wedge just comes back.
- Rotate by replacing the repo secret value. No workflow edit needed.

## Related

- GOL-2504 (this), GOL-2503 / PR #720 (the instance that surfaced it)
- GOL-2114, GOL-2113 — same class in `grove-odoo-modules` (fix: PR #182)
- GOL-1958 — the missing-required-check sweep that files the follow-on issues
- GOL-1478 — SHA-bound protected-paths-guard approval (unchanged by this)
