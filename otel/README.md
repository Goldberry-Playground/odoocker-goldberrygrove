# Grove OTel Collector — USE / infra metrics

Ships the "how hard is each resource working" half of observability (spec §3):
host CPU/RAM/disk/network + per-container CPU/RAM → OpenObserve, as OTLP metrics.
Joined with the cost-bridge's `cost_resource_*`, this is what turns the CostOps
dashboard from "what's expensive" into "what's expensive **and idle**" (§6).

## How it works

```
beyla (eBPF, Odoo) ──OTLP──┐
                           ▼
otel-collector (contrib image)
  receivers:  hostmetrics (/hostfs) + docker_stats (docker.sock) + otlp (from Beyla)
  processors: resourcedetection → resource (env label) → batch
  exporter:   otlphttp → OpenObserve (basicauth)   [metrics + traces pipelines]
```

The Collector is the single auth'd egress: agents (Beyla, and future Next.js
OTel) export to it plain over the internal network; it adds OpenObserve auth once.

- Runs on the **app/Odoo droplet** (where `/proc` + the Docker socket live).
  The App Platform frontends have no `docker_stats` — their USE metrics come from
  the **DO-metrics bridge** (`do-metrics/`, polls the DO Monitoring API →
  `do_app_*` gauges + `app-*` alerts).
- `OPENOBSERVE_OTLP_BASE` targets local `openobserve:5080/api/default` by default;
  point it at the obs droplet (`https://oo.qa-l3.…/api/default`) in QA/prod.

## Alerts (in `openobserve/alerts.json`)

**USE:** `droplet-cpu-{warning,critical}` (>70/90%), `container-ram-{warning,critical}`
(>80/95% of limit), `disk-data-{warning,critical}` (>75/90% — tighter because
MinIO/Postgres-WAL fail catastrophically when full).

**RED (Beyla):** `odoo-orders-latency-{warning,critical}` (p95 >3s/8s),
`odoo-products-latency-warning` (p95 >1s), `odoo-error-rate-warning` (5xx >1%).

## Beyla (eBPF RED for Odoo)

`grafana/beyla` (privileged + `pid: host`) attaches eBPF probes to the Odoo
process by listen port (8069) and emits RED metrics per HTTP route — **zero Odoo
code**. Exports OTLP to `otel-collector:4318`. Single sidecar (Odoo only —
Managed Postgres has no kernel access; its stats come from the `postgresql`
receiver). Needs a Linux host + compatible kernel; if it can't attach it logs
and produces no metrics (never crashes the stack).

## The `postgresql` receiver (app-plane — GOL-335, LIVE on QA)

The receiver lives in the **app-plane overlay fragment**
`otel/otelcol-config.postgres.yaml`, merged via a second `--config` on the
app-plane collector (`docker-compose.monitoring.app-plane.yml`). It is kept out
of the shared base `otelcol-config.yaml` on purpose — the obs-plane + local
full-stack collectors have no Managed PG, and an unset `POSTGRES_*` in their
pipeline would fail startup and take the USE/RED metrics down with it. On ADR-007
app-plane hosts a Managed PG is always reachable, so the fragment is safe there.

Enabled on `grove-qa-l3-odoo` as follows (repeat for prod):

1. Create a read-only monitoring user on the Managed PG (connect from the Odoo
   droplet — it is a trusted source on the cluster firewall):
   ```sql
   CREATE ROLE otel_monitor LOGIN PASSWORD '…';
   GRANT CONNECT ON DATABASE odoo TO otel_monitor;
   ```
   `pg_monitor` is **not** required for the two connection metrics
   (`postgresql_backends` ← public `pg_stat_database`, `postgresql_connection_max`
   ← `pg_settings`) — and DO's `doadmin` cannot grant `pg_monitor` onward on
   PG16+ anyway. Grant it only if you later add restricted receiver metrics.
2. Set `POSTGRES_ENDPOINT` (managed host **:25060**) / `POSTGRES_MONITOR_USER` /
   `POSTGRES_MONITOR_PASSWORD` in `.env.monitoring`. Creds are in 1Password:
   `Grove Infra/pg_otel_monitor_{user,password}` + `pg_qa_l3_private_host`.
3. Redeploy the app-plane overlay (it already wires the second `--config` +
   mounts the fragment). Then re-run `setup-monitoring.py` to load the
   `postgres-connections-{warning,critical}` alerts.

> **max_connections note:** OpenObserve column conditions can't divide two metric
> streams, so the two alerts threshold on the raw backend count vs a literal
> (dev-tier `max_connections=25` → warn `>17`, crit `>22`). If the Managed PG is
> resized, update those numbers to `0.70 / 0.90 × new max`.

## Not yet (documented follow-ups)

- **Next.js `instrumentation.ts`** (grove-sites) → the `nextjs-*` latency + 5xx
  alerts + BFF→Odoo trace correlation. Separate PR (different repo).
- **Rightsizing join panel** — a computed `cost × utilization` JOIN on the Grove
  CostOps dashboard once these metric stream/field names are confirmed live.

> **Pending live validation:** the OpenObserve OTLP endpoint/auth and the exact
> resulting stream/field names (`system_cpu_utilization`, `container_memory_percent`,
> `system_filesystem_utilization`) are the documented shapes but unexercised —
> confirm on first `make monitoring-up` and adjust the exporter + alert `stream`s.
