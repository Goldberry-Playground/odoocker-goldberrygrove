# Grove OTel Collector — USE / infra metrics

Ships the "how hard is each resource working" half of observability (spec §3):
host CPU/RAM/disk/network + per-container CPU/RAM → OpenObserve, as OTLP metrics.
Joined with the cost-bridge's `cost_resource_*`, this is what turns the CostOps
dashboard from "what's expensive" into "what's expensive **and idle**" (§6).

## How it works

```
otel-collector (contrib image)
  receivers:  hostmetrics (/hostfs) + docker_stats (docker.sock)
  processors: resourcedetection → resource (env label) → batch
  exporter:   otlphttp → OpenObserve (basicauth)
```

- Runs on the **app/Odoo droplet** (where `/proc` + the Docker socket live).
  The App Platform frontends have no `docker_stats` — their USE metrics come from
  the DO-metrics bridge (a separate follow-up).
- `OPENOBSERVE_OTLP_BASE` targets local `openobserve:5080/api/default` by default;
  point it at the obs droplet (`https://oo.qa-l3.…/api/default`) in QA/prod.

## Alerts (in `openobserve/alerts.json`)

`droplet-cpu-{warning,critical}` (>70/90%), `container-ram-{warning,critical}`
(>80/95% of limit), `disk-data-{warning,critical}` (>75/90% — tighter because
MinIO/Postgres-WAL fail catastrophically when full).

## Not yet (documented follow-ups)

- **`postgresql` receiver** — a commented block in the config; enable with a
  read-only `pg_monitor` user → unlocks `postgres-connections` + query stats.
- **Beyla eBPF** — RED metrics (latency/error rate per `/grove/api/v1/*`) for the
  `nextjs-*`/`odoo-*` latency + `5xx-rate` alerts. Separate PR.
- **Rightsizing join panel** — add a `cost × utilization` panel to the Grove
  CostOps dashboard once these metric stream/field names are confirmed live.

> **Pending live validation:** the OpenObserve OTLP endpoint/auth and the exact
> resulting stream/field names (`system_cpu_utilization`, `container_memory_percent`,
> `system_filesystem_utilization`) are the documented shapes but unexercised —
> confirm on first `make monitoring-up` and adjust the exporter + alert `stream`s.
