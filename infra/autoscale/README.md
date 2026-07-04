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

| Tier | Size | ~$/mo | Use |
|------|------|-------|-----|
| `quiet` | `s-2vcpu-2gb` | 18 | Off-peak floor (only if live disk ≤ 60GB — confirm first) |
| `base` | `s-2vcpu-4gb` | 24 | Default resting tier |
| `busy` | `s-4vcpu-8gb` | 48 | Market days / launches / batch runs |

**Reversibility:** resizes never touch the disk. A target whose nominal disk is smaller than the *live* disk is refused (DO cannot shrink disk), so the day-to-day reversible band is `base` ↔ `busy`. The workflow enforces this at runtime.

## Safety model

- **Blast-radius pin:** the workflow refuses any droplet id other than `572389418`.
- **Allowlist:** only slugs in `agenticos-tiers.json.allowlist` are permitted.
- **Reversible:** `--resize-disk=false` always; disk-shrink targets refused.
- **Human-gated:** `workflow_dispatch` = a human click. `schedule` = notify-only unless `AUTOSCALE_SCHEDULED_APPLY=true` **and** the `agenticos-resize` Environment has a required reviewer.
- **Scoped secrets:** `DIGITALOCEAN_TOKEN` + `DISCORD_OPS_WEBHOOK_URL` fetched at runtime via Infisical OIDC (ADR-003) — no long-lived secrets in the repo.

## Prerequisites / status

- **Requires** a DO API token with `droplet:read+write` scope in Infisical `grove-odoocker-qu8p` prod (provisioned by **P0 / GOL-53**). Until then the workflow is committed but cannot apply.
- The `agenticos-memory-*-capacity` alerts require the AgenticOS OTel hostmetrics stream from **P1 / GOL-54**; until that ships, the alerts are inert (no metric to evaluate) but harmless.
- **First-run TODO (needs the P0 token):** confirm the live baseline and correct the ladder if it differs:
  ```
  doctl compute droplet get 572389418 --format Name,Memory,VCPUs,Disk,Size
  ```
  The workflow's runtime guards make a stale slug **fail safe** (refuse), never misfire.
