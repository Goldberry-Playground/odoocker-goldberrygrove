# AgenticOS droplet — P0 OOM mitigation runbook (GOL-53)

Incident-mitigation kit for the **AgenticOS droplet** (`agenticos` / `Production`,
DO id **`572389418`**, region `nyc1`, ~2 vCPU) after it OOM-crashed at RAM ~103%.
Parent strategy: **GOL-51** (`Autoscaling strategy`) → this is the **P0** child (**GOL-53**).

Everything here is **idempotent, reversible, and codified** so the moment access lands
it's a one-command apply, not a snowflake console session.

## Order of operations (matters)

Guardrails first (prevent recurrence), then headroom, then observability:

| # | Step | File | Needs | Cost | Downtime |
|---|------|------|-------|------|----------|
| 1 | Swap file (4G, swappiness=10) | `01-swap.sh` | SSH to host (root) | $0 | none |
| 2 | Docker `mem_limit`/`mem_reservation` | `docker-compose.agenticos-limits.yml` | SSH to host | $0 | container restart |
| 3 | Right-size one tier up ⚠️ **board-gated** | `05-resize.sh` | DO token (`droplet:rw`) | +$6–24/mo | ~1–2 min |
| 4a | Install `do-agent` (host metrics) | `03-do-agent-install.sh` | SSH to host (root) | $0 | none |
| 4b | DO alert policies → Discord | `04-do-alert-policies.sh` | DO token (`monitoring:rw`) | $0 | none |

Steps **1, 2, 4a** need **SSH to `572389418`**. Steps **3, 4b** need a **write-scoped DO API
token** (no SSH). They are independent — whichever access lands first, that half can run.

## What I need to execute (unblock — owner: CEO - Rick)

This DevOps execution env has **no DO token, no SSH key, no `doctl`/`docker`**. To run this
kit myself (not hand to a human) I need **one or both** of:

1. **Write-scoped DO API token** in my env as `DO_API_TOKEN` — scopes `droplet:read+write`
   + `monitoring:read+write`. Unblocks steps **3 + 4b** (resize + alerts). Short-lived
   preferred; I'll use it and it can be revoked after.
2. **SSH access to `572389418`** (pubkey added, or a cloud-init re-provision path).
   Unblocks steps **1, 2, 4a** (swap + mem_limits + do-agent).

Plus: the **ops Discord webhook URL** as `OPS_DISCORD_WEBHOOK` (for step 4b) — same channel
family as `DISCORD_WEBHOOK_CRITICAL` in `.env.monitoring`.

Plus: **board approval for the resize** (step 3 only — spend + brief downtime). Steps 1/2/4
are $0 and reversible and need no spend approval.

## Run order (once unblocked)

```bash
# On the AgenticOS host (root), after SSH lands:
sudo bash infra/agenticos/01-swap.sh
docker ps --format '{{.Names}}\t{{.Image}}'        # confirm real agent-runner names
$EDITOR infra/agenticos/docker-compose.agenticos-limits.yml   # rename placeholder services
docker compose -f <base-compose> -f infra/agenticos/docker-compose.agenticos-limits.yml up -d
sudo bash infra/agenticos/03-do-agent-install.sh

# From any env with the DO token (no SSH needed):
DO_API_TOKEN=... OPS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy \
  bash infra/agenticos/04-do-alert-policies.sh

# Board-approved only:
DO_API_TOKEN=... TARGET_SIZE=s-2vcpu-4gb APPROVAL_REF=<approval-id> \
  bash infra/agenticos/05-resize.sh
```

## Verification (smallest proof per step)

- **Swap:** `swapon --show` shows `/swapfile` 4G; `sysctl vm.swappiness` = 10; `free -h` shows swap.
- **mem_limits:** `docker inspect <runner> --format '{{.HostConfig.Memory}}'` = `1572864000` (1500m);
  `docker stats --no-stream` shows a `LIMIT` column, not `/ <hostRAM>`.
- **do-agent:** `systemctl is-active do-agent` = `active`; metrics visible in DO panel within ~2 min.
- **DO alerts:** `04-...sh` prints the 3 `[GOL-53]` policies; fire a test breach and confirm it
  lands in the ops Discord channel.
- **resize:** script prints new `size_slug`/`memory`; Healthchecks ping recovers; `:1933` UI loads.

## Rollback

Every step has an inline rollback block. Summary:
- swap: `swapoff /swapfile && rm /swapfile`, strip fstab + sysctl lines.
- mem_limits: `docker compose ... up -d` without the overlay file.
- do-agent: `curl -sSL https://repos.insights.digitalocean.com/uninstall.sh | bash`.
- alerts: delete `[GOL-53]` policies by uuid (script prints them).
- resize: re-run `05-resize.sh` with the previous slug (CPU/RAM resize is reversible).

## Notes / decisions

- **Discord via Slack bridge:** DO alert policies don't natively support Discord. Discord
  incoming webhooks accept Slack-formatted payloads at `<webhook>/slack`, so `04-...sh`
  registers the Discord webhook (with `/slack` suffix) as the policy's Slack channel. No
  extra infra, fully supported.
- **Why native DO alerts AND OpenObserve (P1)?** Two independent detection paths by design
  (ADR: separate failure domains). DO alerts are the fast, obs-droplet-independent layer;
  P1 (GOL-54) adds the rich OpenObserve single-pane. See the GOL-51 plan doc.
- **Not autoscale:** DO can't vertically autoscale a single stateful droplet. Scheduled/
  threshold vertical resize (human-gated) is P2 (GOL-55). See GOL-51 plan §2.
