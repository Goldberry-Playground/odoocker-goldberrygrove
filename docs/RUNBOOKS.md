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

## smoke-test-no-action-needed

**Severity:** test · **Likely impact:** none.

This is what `scripts/smoke-test-monitoring.sh` sends as a connectivity probe. If it arrives in Discord, the wiring works. Ignore it.

---

# USE (resource) runbooks

> These fire from the OTel Collector hostmetrics/docker_stats streams. Two hosts
> share the same OpenObserve streams and are separated by `host_role`:
> `odoo-droplet` (the app/Odoo box, `otel/otelcol-config.yaml`) and `agenticos`
> (the AgenticOS/paperclip-server box, AgenticOS repo `infra/otel/otelcol-agenticos.yaml`, GOL-54).
> DO-native alerts (`monitor-alerts.tf`, P0) are the redundant fast layer for the
> agenticos host. Thresholds are pending live validation against the obs-droplet.

## droplet-cpu

**Severity:** warning >70% / critical >90% (10m) · **Host:** odoo-droplet · **Likely impact:** slow storefront + Odoo responses; risk of container thrash.

### Check first
1. Which container is hot: OpenObserve `container_cpu_utilization{host_role='odoo-droplet'}` top-N (or on the box: `docker stats --no-stream`).
2. Is it steady-state load or a runaway (a stuck Odoo worker / cron)?

### Common fixes
- **Runaway Odoo worker:** `make restart-odoo`.
- **Sustained real load:** scale the droplet (see `05-resize.sh` in the odoocker infra kit) — board-gated spend.

### Escalation
If both warning + critical hold >30m with no single hot container, it's genuine capacity — escalate for a resize.

---

## container-ram

**Severity:** warning >80% / critical >95% of limit (5m, any container) · **Host:** odoo-droplet · **Likely impact:** OOM kill of the named container (Odoo workers most likely).

### Check first
1. `docker stats --no-stream` — confirm the `{container_name}` and its limit.
2. `dmesg | tail` for a prior "killed process" (OOM) on that container.

### Common fixes
- **Odoo worker leak:** `make restart-odoo`; if recurrent, lower `--limit-memory-hard`/worker count or raise the container `mem_limit` if the box has headroom.
- **Wrong limit:** adjust `mem_limit` in `docker-compose.override.production.yml`.

### Escalation
Recurrent critical on the same container = right-size the limit or the droplet; escalate the spend.

---

## disk-data

**Severity:** warning >75% / critical >90% · **Host:** odoo-droplet · **Likely impact:** near-full data volume kills MinIO writes + Postgres WAL (silent-then-catastrophic).

### Check first
1. On the box: `df -h /data /` and `du -sh /data/* | sort -h | tail`.
2. Usual culprits: MinIO objects (OpenObserve Parquet), Postgres WAL, Docker images/logs.

### Common fixes
- **Docker bloat:** `docker system prune -f` (safe) / `docker image prune -a`.
- **MinIO/Parquet growth:** confirm OpenObserve retention; offload to DO Spaces.
- **Volume genuinely full:** attach/grow a DO block volume.

### Escalation
Critical (>90%) is act-now — a full volume corrupts Postgres WAL. Clear space or grow the volume immediately.

---

## agenticos-cpu

**Severity:** warning >70% / critical >90% (10m) · **Host:** agenticos · **Likely impact:** slow/stalled agent runs; risk of thrash.

### Check first
1. Hot container via OpenObserve `container_cpu_utilization{host_role='agenticos'}` (usually `paperclip-server`, which runs every agent subprocess).
2. Runaway agent loop vs genuine concurrency (market season).

### Common fixes
- **Runaway container:** restart it on the box.
- **Sustained real load:** resize the AgenticOS droplet — see [GOL-51](/GOL/issues/GOL-51) autoscaling; board-gated spend.

### Escalation
Sustained critical with no single culprit = capacity; escalate a resize via GOL-51.

---

## agenticos-memory

**Severity:** warning >70% / critical >90% host RAM (10m) · **Host:** agenticos · **Likely impact:** OOM kill — this box crashed the UI at 103% (the reason GOL-51 exists).

### Check first
1. `system_memory_utilization{host_role='agenticos'}` trend + `system_paging_utilization` (swap is the P0 safety net — is it engaging?).
2. Top container by `container_memory_percent{host_role='agenticos'}` (usually `paperclip-server`).

### Common fixes
- **Swap absorbing it:** confirm the 4G swap file is active (`swapon --show`); it buys time, not a fix.
- **Hot container:** restart it; if recurrent, the box needs more RAM.

### Escalation
Critical = imminent OOM. If swap is already saturated, resize the droplet now (GOL-51). The DO-native alert (`monitor-alerts.tf`) covers this same signal as the fast redundant layer.

---

## agenticos-container-ram

**Severity:** warning >80% / critical >95% of limit (5m, any container) · **Host:** agenticos · **Likely impact:** OOM kill of the named container.

### Check first
1. `docker stats --no-stream` on the box — confirm `{container_name}` + limit.
2. `dmesg | tail` for a prior OOM kill.

### Common fixes
- **paperclip-server growth:** restart it; if recurrent, raise its `mem_limit` (if the host has headroom) or lower agent concurrency.

### Escalation
Recurrent critical on the same container = right-size the limit or the droplet (GOL-51).

---

## agenticos-disk

**Severity:** warning >75% / critical >90% root filesystem · **Host:** agenticos · **Likely impact:** a full disk stalls Docker + the agent runtime.

### Check first
1. `df -h /` and `du -sh /* 2>/dev/null | sort -h | tail` on the box.
2. Usual culprits: Docker images/logs, the swap file, agent workspaces.

### Common fixes
- **Docker bloat:** `docker system prune -f` / `docker image prune -a`.
- **Log growth:** check journald / container log rotation.

### Escalation
Critical (>90%) is act-now — free space or grow the volume before Docker/the runtime stalls.
