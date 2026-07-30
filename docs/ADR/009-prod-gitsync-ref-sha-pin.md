# ADR 009: Production pins `grove-odoo-modules` to a SHA, not `main` (`GITSYNC_REF` supply-chain pin)

**Status:** Accepted
**Date:** 2026-07-30
**Deciders:** Josh Dunbar (board)
**Origin:** board decision recorded in GOL-984 triage (underlying Asana `1215643893776947`); formalized here as an ADR record per the GOL-989 stale-docs sweep.

## Context

Custom Odoo modules (`grove_headless` et al.) live in the separate
[`grove-odoo-modules`](https://github.com/Goldberry-Playground/grove-odoo-modules)
repo ([ADR-001](./001-separate-modules-repo.md)) and reach a running Odoo host
through a **git-sync sidecar** rather than being baked into the image. The
sidecar clones the modules repo at a ref supplied as `GITSYNC_REF`
(`docker-compose.yml`; `GITSYNC_REF` supersedes the deprecated
`GITSYNC_BRANCH` in git-sync v4).

That ref is the single lever that decides *which module code runs*. It can
point at a moving branch (`main`) or a fixed commit SHA. The two behave very
differently for production:

- **Branch (`main`)** — any merge to `grove-odoo-modules` `main` is picked up
  on the next sync with **no odoocker change and no review of the deploy**. Good
  for QA velocity; unacceptable for prod, where an unreviewed upstream merge
  would silently change production business logic.
- **SHA** — the running code is frozen to an explicit commit. Advancing it is a
  deliberate, reviewed act.

QA already tracks `main` (fast iteration; see [ADR-004](./004-qa-promotion-model.md)).
The open question this ADR settles is what **production** should track.

## Decision

**Production pins `GITSYNC_REF` to a specific `grove-odoo-modules` commit SHA —
never `main`.** Advancing the pin is an explicit, reviewed infra PR in this
(odoocker) repo that edits the `GITSYNC_REF=<sha>` line in
`docker-compose.override.production.yml`.

Concretely:

| Environment | `GITSYNC_REF` source | Bump mechanism |
|---|---|---|
| Local | n/a — modules bind-mounted (`override.local.yml`) | edit the mount |
| Sandbox / QA | `main` (polling) | automatic on merge to modules `main` |
| **Production** | **fixed SHA** in `docker-compose.override.production.yml` | **reviewed odoocker infra PR** |

`GITSYNC_REF=main` in the production override is prohibited — it defeats the pin
and bypasses code review for prod module changes. The bump workflow (confirm SHA
→ smoke-test in sandbox → infra PR listing the included module PRs → merge +
redeploy) is documented in
[`docs/DEPLOYMENT.md` → "Bumping the prod modules SHA"](../DEPLOYMENT.md).

## Why

- **Supply-chain hardening.** Every line of code that runs in production passed
  through a reviewed odoocker commit. There is no path for an upstream merge to
  reach prod without a human approving the exact SHA. This is the same principle
  that keeps prod topology change-controlled in [ADR-007](./007-level-3-app-platform-migration.md).
- **Bisectable, auditable deploy history.** The odoocker git log of
  `GITSYNC_REF` bumps *is* the production module-deploy timeline — each bump PR
  names which module PRs it carries, so "what changed in prod and when" is a
  `git log`, not archaeology across two repos.
- **Reversible.** Rollback is reverting the pin to the prior SHA and
  redeploying — no cherry-picking in the upstream repo.
- **QA keeps its velocity.** Pinning is prod-only; QA/sandbox stay on `main` so
  iteration speed is unaffected. The SHA a bump PR proposes is exactly what QA
  already exercised, keeping QA→prod code-identical per ADR-004.

## Consequences

**Positive:**
- Prod module changes are gated by the same review + CI as any infra change.
- The prod pin doubles as a supply-chain attestation: the deployed SHA is
  greppable in-repo (`docker-compose.override.production.yml`).

**Negative / open:**
- Prod does **not** auto-pick-up module fixes — an urgent fix still needs a bump
  PR. Accepted: the reviewed-deploy guarantee is worth the extra step, and the
  bump is a one-line PR.
- Two SHAs must be kept honest: the pin in `docker-compose.override.production.yml`
  and whatever the live droplet last synced. Drift between "merged pin" and
  "applied pin" is possible because prod applies are manual (ADR-007; the
  README `validation` block guards format, not liveness) — reconcile via the
  DEPLOYMENT bump procedure, and codify any hand-applied pin back into the
  override so the repo stays the source of truth.

## References

- [ADR-001](./001-separate-modules-repo.md) — why modules live in a separate repo
- [ADR-004](./004-qa-promotion-model.md) — QA promotion model; QA tracks `main`, prod is SHA-pinned end-to-end
- [ADR-007](./007-level-3-app-platform-migration.md) — Level-3 topology these prod pins deploy onto
- `docs/DEPLOYMENT.md` → "Bumping the prod modules SHA" — the operator workflow
- `docker-compose.override.production.yml` — where the live pin lives
