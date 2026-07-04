# ADR-009: Human-gated vertical resize for the AgenticOS droplet ("no autonomous prod apply")

**Status:** Accepted
**Date:** 2026-07-04
**Owner:** DevOps - Terra
**Context issues:** [GOL-51](../../) (Autoscaling strategy) · GOL-55 (this implementation) · GOL-53 (P0 mitigation) · GOL-54 (P1 observability)
**Relates to:** ADR-008 (observability — OpenObserve supersedes ADR-004's monitoring assumptions)

## Context

The founder asked for the AgenticOS control-plane droplet (DO id `572389418`, `nyc1`) to *"scale up on busy days, scale down when quiet"* after it OOM-crashed at ~103% RAM.

The honest constraint (see the GOL-51 strategy):

- **DigitalOcean cannot vertically autoscale a single Droplet.** Changing CPU/RAM is a **resize** (power-off → resize → power-on, ~1–2 min downtime). CPU/RAM resize is reversible; **disk resize is one-way/permanent**.
- **DO Droplet Autoscale Pools** scale a *horizontal fleet of identical stateless droplets* behind a load balancer — not a singleton **stateful** control plane (Postgres + run/agent state). They do not apply here without a re-platform.
- True autoscale (App Platform / DOKS) is a horizontal-only re-platform project, deferred (GOL-51 Phase 3).

Therefore the realistic autoscale is a **scheduled/threshold-driven vertical resize**, codified as IaC. The remaining decision is **who is allowed to apply a prod resize, and how**.

## Decision

**No autonomous apply to production. A human action is always the gate for any AgenticOS resize.**

We adopt a tiered model (from the observability spec):

- **Tier-0 / Tier-1 — human-gated apply (this ADR's default).** Resizes are applied only via a GitHub Actions **`workflow_dispatch`** — i.e. a person clicking *Run workflow* (including the one-click link embedded in the OpenObserve → Discord capacity alert). The click is the gate. The workflow is reviewed IaC in-repo (`.github/workflows/agenticos-autoscale.yml`), reversible (`--resize-disk=false`), blast-radius-pinned to `572389418`, and allowlist-guarded to a fixed tier ladder (`infra/autoscale/agenticos-tiers.json`).
- **Scheduled trigger — notify-only by default.** The `schedule` cron does **not** apply to prod. It posts a Discord reminder with the one-click link. It may apply autonomously **only** if *both* the repo variable `AUTOSCALE_SCHEDULED_APPLY=true` **and** the `agenticos-resize` GitHub Environment has a required reviewer — so even the opted-in "scheduled" path still pauses for a human.
- **Tier-2 — autonomous apply — reserved for zero-blast-radius targets only** (e.g. idle-QA teardown, ephemeral preview droplets). The stateful prod control plane is explicitly **out of scope** for Tier-2.

## Consequences

- The founder gets the requested behaviour (bigger box on market days, smaller when quiet) — deterministic, reversible, cost-visible, and it **cannot surprise-bill** or self-inflict downtime.
- Every resize is an auditable GitHub Actions run + Discord summary, with `doctl` under a scoped DO token brokered by Infisical OIDC (no long-lived secrets in the repo — consistent with ADR-003).
- Optional dual-control is a GitHub-UI toggle (add a required reviewer to the `agenticos-resize` Environment); no code change.
- **Disk reversibility caveat:** because resizes keep the current disk, a target tier whose nominal disk is smaller than the live disk is refused (DO cannot shrink disk). The workflow enforces this at runtime, so the practical reversible band is the set of tiers that share the current disk footprint (intended day-to-day band: `base` ↔ `busy`).

## Alternatives rejected

- **Autonomous threshold-triggered resize** (metric → auto-resize prod): rejected — a mis-scoped alert or metric glitch could power-cycle the control plane and/or run up cost with no human in the loop. Violates least-surprise + blast-radius principles for a singleton stateful box.
- **DO Autoscale Pool / horizontal autoscale now:** rejected for this issue — requires re-platforming the stateful control plane; deferred to GOL-51 Phase 3.
