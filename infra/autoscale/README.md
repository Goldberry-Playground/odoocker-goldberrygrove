# AgenticOS Autoscale — human-gated vertical resize

The realistic "autoscale" for the **AgenticOS control-plane droplet** (`agenticos`, DO id `572389418`, `nyc1`) — a single **stateful** box that DigitalOcean cannot vertically autoscale. Instead of magic, this is **scheduled/threshold-prompted, human-gated vertical resize** codified as IaC.

- **Strategy:** [GOL-51](/GOL/issues/GOL-51) plan · **Implementation:** GOL-55
- **Governance:** [ADR-009](../../docs/ADR/009-human-gated-vertical-autoscale.md) — *no autonomous prod apply.*

## Parts

| File | Role |
|------|------|
| `.github/workflows/agenticos-autoscale.yml` | The resize workflow: `workflow_dispatch` (applies) + `schedule` (notify-only). Uses `doctl` via Infisical OIDC. |
| `infra/autoscale/agenticos-tiers.json` | Tier ladder → DO size slugs, blast-radius pin, allowlist, reversibility rule. **Only supported way to change what the workflow can resize to.** |
| `openobserve/alerts.json` → `agenticos-memory-high-capacity` / `-low-capacity` | The threshold signals (>85% busy / <20% quiet) that fire → Discord with the one-click link. |
| `docs/RUNBOOKS.md#agenticos-capacity` | Operator runbook (the one-click action + checks). |

## How to scale (the one action)

1. A capacity alert lands in Discord (or you just know it's a market day), with a **▶ one-click link**.
2. Open **Actions → AgenticOS Autoscale → Run workflow**.
3. Pick a **tier**: `busy` (up) or `base` (down). Add a reason.
4. It resizes (`--resize-disk=false`, CPU/RAM only, ~1–2 min power-cycle) and posts a before/after summary to Discord.

That's it. The click is the gate — nothing resizes prod without a human.

## Tier ladder

| Tier | Size | disk | ~$/mo | Use |
|------|------|------|-------|-----|
| `quiet` | `s-2vcpu-2gb` | 60GB | 18 | ⛔ Unreachable from the current 80GB disk (can't shrink) — reference only |
| `base` | `s-2vcpu-4gb` | 80GB | 24 | ✅ **Current live tier** — default resting state |
| `busy` | `s-4vcpu-8gb` | 160GB | 48 | Market days / launches / batch runs |

Verified against the live DO API **2026-07-04**: droplet `572389418` is `s-2vcpu-4gb` / 80GB / $24 in `nyc1` (= `base`); all three slugs exist and are available in nyc1 with exactly these specs.

**Reversibility:** resizes never touch the disk. A target whose nominal disk is smaller than the *live* disk is refused (DO cannot shrink disk), so the day-to-day reversible band is `base` ↔ `busy` (both keep the 80GB disk). `quiet` (60GB) is therefore not reachable while the box runs an 80GB disk; the workflow refuses it with a clear disk-shrink message. The workflow enforces all of this at runtime.

## Safety model

- **Blast-radius pin:** the workflow refuses any droplet id other than `572389418`.
- **Allowlist:** only slugs in `agenticos-tiers.json.allowlist` are permitted.
- **Reversible:** `--resize-disk=false` always; disk-shrink targets refused.
- **Human-gated:** `workflow_dispatch` = a human click. `schedule` = notify-only unless `AUTOSCALE_SCHEDULED_APPLY=true` **and** the `agenticos-resize` Environment has a required reviewer.
- **Scoped secrets:** `DIGITALOCEAN_TOKEN` + `DISCORD_OPS_WEBHOOK_URL` fetched at runtime via Infisical OIDC (ADR-003) — no long-lived secrets in the repo.

## Prerequisites / status

- **DO API token — ✅ granted** (GOL-59, `droplet:read+write`; the same token is available to CI via Infisical OIDC as `DIGITALOCEAN_TOKEN`). The workflow can apply once merged to `main`.
- **Live baseline — ✅ verified 2026-07-04** (see the tier-ladder note above). No ladder correction was needed. The workflow's runtime guards additionally re-fetch the live size/disk on every run, so the ladder can't drift into a misfire — a stale slug just fails safe (refuse).
- The `agenticos-memory-*-capacity` alerts require the AgenticOS OTel hostmetrics stream from **P1 / GOL-54**; until that ships, the alerts are inert (no metric to evaluate) but harmless.
- **CI cannot dispatch a workflow that isn't on the default branch** — the `workflow_dispatch` one-click path goes live only after this PR merges to `main`.
