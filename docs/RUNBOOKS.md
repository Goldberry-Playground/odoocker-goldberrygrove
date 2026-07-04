# Grove Runbooks

One entry per alert key emitted by OpenObserve and routed through Keep. Each Discord notification links here via the `runbook_key` field. Keep the table in this file aligned with the `runbook_key` values in `openobserve/alerts.json`.

| `runbook_key` | Severity | Source alert |
|---|---|---|
| [`frontend-degraded`](#frontend-degraded) | warning | `frontend-down-warning` |
| [`frontend-down`](#frontend-down) | critical | `frontend-down-critical` |
| [`odoo-degraded`](#odoo-degraded) | warning | `odoo-degraded-warning` |
| [`odoo-down`](#odoo-down) | critical | `odoo-down-critical` |
| [`ghost-down`](#ghost-down) | warning | `ghost-down-warning` |
| [`postgres-down`](#postgres-down) | critical | `postgres-down-critical` |
| [`ssl-expiring`](#ssl-expiring) | warning | `ssl-expiring-warning` |
| [`agenticos-cpu`](#agenticos-cpu) | warning/critical | `agenticos-cpu-{warning,critical}` |
| [`agenticos-memory`](#agenticos-memory) | warning/critical | `agenticos-memory-{warning,critical}` |
| [`agenticos-container-ram`](#agenticos-container-ram) | warning/critical | `agenticos-container-ram-{warning,critical}` |
| [`agenticos-disk`](#agenticos-disk) | warning/critical | `agenticos-disk-{warning,critical}` |
| [`smoke-test-no-action-needed`](#smoke-test-no-action-needed) | (test) | smoke-test-monitoring.sh |

---

## frontend-degraded

**Severity:** warning · **Likely impact:** users see slow page loads or intermittent 5xx; cart flow may still work.

### Symptoms
- One of `hub-root`, `goldberry-shop`, `ggg-blog`, `nursery-shop` (etc.) returns non-2xx for >60s, OR p95 response time >5s for 2+ checks.
- Discord warning channel shows `⚠️ Grove warning — frontend-down-warning`.

### Check first
1. `docker logs grove-{tenant}` — look for the actual error (Next.js stack trace, fetch error, OOM).
2. Hit the failing route directly in a browser. Does it ALSO 5xx? If yes → real issue. If browser works → likely a synthetic-monitor flake (transient network blip from the OpenObserve container; auto-recovers).
3. Is the backend (Odoo or Ghost) the culprit? Check `odoo-degraded` and `ghost-down` alerts — if either is also firing, fix that first; this one will clear on its own.

### Common fixes
- **Stale Next.js build cache:** `make frontends-build && make frontends-up` (full rebuild).
- **Backend not ready after restart:** wait 30-60s; the storefronts fall back to mock data on Odoo error but Next.js needs a render pass to re-fetch.
- **Tenant-specific env missing:** check `.env.frontends` has all 4 `GHOST_CONTENT_KEY_*` + `HUB_GHOST_CONTENT_API_KEY` + `ODOO_API_KEY` set.

### Escalation
If the alert persists >10 min and a browser also 5xx's: this becomes `frontend-down`. Restart the affected container: `docker restart grove-{tenant}`.

---

## frontend-down

**Severity:** critical · **Likely impact:** users see no page at all on the affected tenant. Discord critical channel pings `@here`.

### Symptoms
- Root route of one tenant unreachable (timeout or connection-refused) for >30s.
- Container may have crashed (OOM, panic) or never started.

### Check first
1. `docker ps -a --filter name=grove-{tenant}` — is the container running, exited, or restarting?
2. If exited: `docker logs grove-{tenant} --tail 100` for the last error.
3. If restarting: it's crash-looping — read the logs to find the root cause.

### Common fixes
- **Crashed at boot due to missing env var:** check `.env.frontends` vs `.env.frontends.example` — every `${VAR}` referenced by the compose file must be defined.
- **Image is broken from a recent rebuild:** roll back with `docker compose -f docker-compose.frontends.yml --env-file .env.frontends up -d --force-recreate grove-{tenant}`.
- **Port collision on host:** `lsof -iTCP:3001-3003 -sTCP:LISTEN` — if a host process took the port, kill it.

### Escalation
If all 4 frontends are down simultaneously, it's likely the Docker engine itself (OrbStack hang). Restart OrbStack from the menu bar, then `make all-up`.

---

## odoo-degraded

**Severity:** warning · **Likely impact:** storefronts may show empty product lists or stale data; cart flow may be slow.

### Symptoms
- Odoo `/web/login` returns non-(2xx,303) for >60s OR response time >10s for 3+ checks.

### Check first
1. `make logs s=odoo` — look for "Database connection refused" or "WorkerPool" warnings (overload).
2. `docker stats gatheratthegrove-odoo-1` — is CPU pegged at 100%?
3. Is Postgres OK? Check `postgres-down` alert; if Postgres is degraded, fix that first.

### Common fixes
- **Worker count too low:** edit `.env` `WORKERS=N` (default 0 = dev mode = 1 worker). Increase to 2-4 for load testing.
- **DB connection saturation:** check Postgres pool settings; increase `max_connections` if the load is real.
- **Slow query:** `make exec s=postgres` then `SELECT pid, query, age(now(), query_start) FROM pg_stat_activity WHERE state = 'active' ORDER BY age DESC LIMIT 5;` to see long-running queries.

### Escalation
If degradation persists >10 min: restart Odoo. `make restart-odoo`. If that doesn't help: full stack restart `make all-down && make all-up`.

---

## odoo-down

**Severity:** critical · **Likely impact:** all storefront commerce (cart, checkout, product fetch) returns errors. Storefronts show fallback "no products" UI. Discord critical channel pings `@here`.

### Symptoms
- Odoo `/web/login` unreachable for >30s.
- Probably also: `frontend-degraded` firing on all 4 storefronts.

### Check first
1. `docker ps --filter name=gatheratthegrove-odoo-1` — is it running?
2. If not: `docker ps -a --filter name=gatheratthegrove-odoo-1` to see if it exited.
3. `make logs s=odoo --tail 50` for the last error.

### Common fixes
- **Postgres not reachable:** Odoo refuses to start without DB. Check `postgres-down` alert first.
- **Missing required env:** if `.env` was edited recently, run `make stack-up` to regenerate `odoo.conf` from template via entrypoint.
- **OOM kill:** check `dmesg | tail` (or OrbStack logs) for "killed process". Increase the container's memory limit in `docker-compose.override.production.yml` if real.

### Escalation
`make restart-odoo` (takes ~1s). If it crash-loops, capture `docker logs gatheratthegrove-odoo-1` for the traceback and escalate before retrying.

---

## ghost-down

**Severity:** warning · **Likely impact:** affected tenant's `/blog` page returns empty state. Commerce flow unaffected.

### Symptoms
- One of `ghost-goldberry-admin`, `ghost-ggg-admin`, `ghost-nursery-admin` unreachable for >60s.

### Check first
1. `docker ps --filter name=gatheratthegrove-ghost-{tenant}-1`
2. `docker logs gatheratthegrove-ghost-{tenant}-1 --tail 30` — Ghost is usually verbose about why it failed to start.

### Common fixes
- **Volume not mounted correctly:** if you just nuked + recreated, run `make ghost-setup-{tenant}` to re-create the admin + Content API key.
- **Port collision:** Ghost wants 2368/2369/2370. If another process took those, fix the conflict.
- **Ghost migration in progress:** if you just upgraded the image, Ghost may be running migrations. Wait 1-2 min; auto-recovers.

### Escalation
`docker restart gatheratthegrove-ghost-{tenant}-1`. If post-restart Ghost still fails, may need to nuke the volume and re-run `make ghost-setup-{tenant}`.

---

## postgres-down

**Severity:** critical · **Likely impact:** everything backend-dependent breaks. Odoo refuses to serve any request. Discord critical channel pings `@here`.

### Symptoms
- TCP probe to `postgres:5432` fails for >30s.

### Check first
1. `docker ps --filter name=gatheratthegrove-postgres-1`
2. `docker logs gatheratthegrove-postgres-1 --tail 100` — look for "out of disk space," "permission denied," or "WAL archive failed."
3. `docker exec gatheratthegrove-postgres-1 df -h /var/lib/postgresql/data` — disk full is the #1 cause.

### Common fixes
- **Disk full:** vacuum + free up space. The `pg-data` named volume is unbounded — if Odoo logs are filling it, configure log rotation.
- **Corrupt WAL:** `docker exec gatheratthegrove-postgres-1 pg_resetwal` (DANGEROUS — only after a backup).
- **Crashed during a transaction:** restart usually recovers. `docker restart gatheratthegrove-postgres-1`.

### Escalation
If Postgres won't start at all, you may need to roll back to a sanitized snapshot from DO Spaces (see `scripts/preview/post-restore-purge.sql` for the restore flow once M1 is operational).

---

## ssl-expiring

**Severity:** warning · **Likely impact:** none yet — preventive alert.

> **Deferred:** SSL monitors fail-to-evaluate locally because `localhost` has no certs. This runbook applies once the M2 droplet ships with real certs.

### Symptoms
- One of the 4 public hostnames has a cert expiring in <14 days.

### Check first
1. Confirm with `openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | openssl x509 -noout -dates`
2. Is automatic Let's Encrypt renewal stuck? Check the acme-companion logs once M2 lands.

### Common fixes
- **Let's Encrypt rate-limit:** check the acme-companion logs for "429 Too Many Requests".
- **Cert renewal hook misfired:** manually run renewal: `docker exec acme-companion /app/force_renew`.

---

## agenticos-cpu

**Severity:** warning (>70%/10m) · critical (>90%/10m) · **Likely impact:** agent runs slow; at critical, control-plane (Paperclip API / heartbeats) starves.

The **AgenticOS droplet** (DO id `572389418`, nyc1) runs the agent fleet + Paperclip control plane. USE metric from the standalone AgenticOS OTel Collector (`infra/agenticos/`, `host_role='agenticos'`).

### Check first
1. Which container is hot: `ssh` to the box → `docker stats --no-stream` (or the `container_cpu_utilization` panel filtered `host.role=agenticos` in OpenObserve).
2. Is it a single runaway agent run vs. broad load? Cross-check the DO-native CPU alert (P0/GOL-53) — two independent paths agreeing = real.

### Common fixes
- **Single runaway run:** stop/restart that agent-runner container; it will bounce (`restart: unless-stopped`).
- **Sustained broad load (busy day):** trigger the scheduled/threshold vertical resize up (GOL-55, `agenticos-scale` workflow) — reversible, ~1–2 min reboot.
- **Chronic:** right-size the base tier (GOL-53 P0.3, board-gated cost).

### Escalation
DevOps - Terra. Cost/resize approval → CEO - Rick.

---

## agenticos-memory

**Severity:** warning (>70%/10m) · critical (>90%/10m) · **Likely impact:** THIS is the alert that would have caught the original OOM crash (host RAM ~103%, UI down).

Host RAM utilization on the AgenticOS box. The three-part fix for the OOM incident is: **swap safety net** (P0.1) + **per-container `mem_limit`** (P0.2) + **this early-warning telemetry** (P1/GOL-54).

### Check first
1. `free -h` on the box — is swap being consumed (P0.1 working as intended) or is RAM genuinely exhausted?
2. `docker stats --no-stream` — which container is the memory hog? Cross-check `agenticos-container-ram`.
3. OpenObserve `system_paging_utilization` panel (`host.role=agenticos`) — heavy swap-in/out = real pressure, not just cache.

### Common fixes
- **One container over its share:** its `mem_limit` (P0.2 overlay) should already cap it; if the *host* is still hot, a limit is missing/too high — retune `infra/agenticos/docker-compose.agenticos-limits.yml`.
- **Genuine headroom shortage:** resize up (GOL-55) or right-size base tier (GOL-53 P0.3).
- **Verify swap exists:** if `free -h` shows 0 swap, P0.1 (`infra/agenticos/01-swap.sh`) hasn't been applied — apply it.

### Escalation
DevOps - Terra. Resize/spend → CEO - Rick.

---

## agenticos-container-ram

**Severity:** warning (>80%/5m) · critical (>95%/5m) · **Likely impact:** a container approaching/hitting its `mem_limit` will be OOM-killed (by design — that's the noisy-neighbor guardrail) and restart.

Per-container memory as a % of its `mem_limit` (docker_stats), scoped `host_role='agenticos'`.

### Check first
1. `{container_name}` in the alert = the offender. `docker inspect <name> --format '{{.HostConfig.Memory}}'` to see its cap.
2. Is it flapping (repeated restarts) or a one-time spike? `docker ps` → check the `STATUS` uptime.

### Common fixes
- **One-time spike:** none — the cap did its job; the container bounced and the host stayed up. Informational.
- **Flapping / OOM-loop:** the `mem_limit` is too tight for that runner's real working set — raise it in `infra/agenticos/docker-compose.agenticos-limits.yml` (keep the sum of caps < RAM+swap with margin) and re-apply.

### Escalation
DevOps - Terra.

---

## agenticos-disk

**Severity:** warning (>75%) · critical (>90%) · **Likely impact:** a full root disk crashes the control plane + all agent runs.

Root filesystem utilization on AgenticOS. The AI storage-growth risk the obs spec flags (agent workspaces, Docker layers, logs accumulating).

### Check first
1. `df -h /` on the box.
2. What's growing: `docker system df` (images/volumes/build cache) and `du -sh /var/lib/docker /root/* /var/log 2>/dev/null | sort -h | tail`.

### Common fixes
- **Docker bloat:** `docker system prune -af --volumes` (careful with `--volumes` — confirm no needed data volumes first).
- **Log growth:** the collector + agent-runner containers use json-file logging with rotation; check any container missing `max-size`.
- **Old agent workspaces:** clear stale run workspaces per the AgenticOS host's cleanup policy.

### Escalation
DevOps - Terra. Persistent growth → consider a dedicated block volume (spec §5 hardening) — CEO - Rick for the ~$1/mo.

---

## smoke-test-no-action-needed

**Severity:** test · **Likely impact:** none.

This is what `scripts/smoke-test-monitoring.sh` sends as a connectivity probe. If it arrives in Discord, the wiring works. Ignore it.

---

## agenticos-capacity

**Severity:** warning (capacity signal, not an incident) · **Likely impact:** none directly — this is the "scale up busy days / down when quiet" prompt.

The AgenticOS droplet (`572389418`) is a **single stateful box** — DigitalOcean cannot vertically autoscale it (see [ADR-009](ADR/009-human-gated-vertical-autoscale.md)). The realistic autoscale is a **human-gated vertical resize**: this alert tells you the box is sustainedly busy (>85% mem / 15m → scale up) or sustainedly idle (<20% mem / 2h → scale down to save cost), and hands you a one-click resize.

### One-click resize (this is the action)
▶ **Run the resize:** <https://github.com/Goldberry-Playground/odoocker-goldberrygrove/actions/workflows/agenticos-autoscale.yml> → **Run workflow**:
- **Busy / >85%:** pick tier **`busy`** (`s-4vcpu-8gb`) before the busy window.
- **Quiet / <20%:** pick tier **`base`** (`s-2vcpu-4gb`) to trim cost.

The resize is reversible (`--resize-disk=false`, CPU/RAM only), takes ~1–2 min (brief power-cycle of the box), and posts a before/after summary back to Discord. No autonomous apply — the click is the gate.

### Check first
1. Confirm it's sustained, not a single agent-run spike: open the AgenticOS memory panel in OpenObserve.
2. Confirm the current tier: `doctl compute droplet get 572389418 --format Name,Memory,VCPUs,Disk,Size`.
3. Cross-check `agenticos-memory-critical` — if that's also firing, this is an incident (see [agenticos-memory](#agenticos-memory)), not just a capacity nudge; consider the P0 swap/mem-limit guardrails first.

### Tiers + config
Ladder lives in `infra/autoscale/agenticos-tiers.json`; mechanism + governance in `infra/autoscale/README.md`.

### Escalation
DevOps - Terra. Cost of the `busy` tier is ~+$24/mo over `base` while active — trim back down when the window passes (the `agenticos-memory-low-capacity` alert reminds you).
