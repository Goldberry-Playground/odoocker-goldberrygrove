# AgenticOS droplet — P1 OpenObserve wiring (GOL-54)

Ships the AgenticOS droplet's **host + container USE metrics** to the separate Grove
**obs-droplet OpenObserve**, closing the gap the Grove Observability design blueprints
(spec §3 / §5) but never built. Parent: **GOL-51** · P0 sibling: **GOL-53** (see
`README.md`) · this is **P1 (GOL-54)**.

> **Layering:** the DO-native alerts from P0 (`03-do-agent-install.sh` + `04-do-alert-policies.sh`)
> are the **fast, obs-droplet-independent** detection layer. This P1 wiring is the **rich,
> single-pane** layer. Two independent paths by design — keep both.

## Files (P1)

| File | What it does |
|---|---|
| `otelcol-agenticos-config.yaml` | OTel Collector config: `hostmetrics` (cpu/load/mem/disk/fs/net/paging) + `docker_stats` (per-container cpu%/mem%) → obs-droplet OpenObserve, tagged `host.role=agenticos`. Trimmed from the odoocker `otel/otelcol-config.yaml` template (no `otlp`/Beyla/`postgresql` receivers — AgenticOS runs none of those). |
| `docker-compose.agenticos-otel.yml` | Standalone compose to run the collector on the box. Joins **no** shared network; reads `/hostfs` + `docker.sock` **read-only**; pushes OTLP **out**. Own `mem_limit: 200m` (the watcher must not OOM the box it watches). |
| `.env.agenticos-otel.example` | Env template: `OPENOBSERVE_OTLP_BASE` (obs-droplet) + ingest creds + `COST_ENV=agenticos`. Copy → `.env.agenticos-otel`, never commit. |

Alert rules + runbooks (in the shared monitoring source-of-truth, not this dir):
- `openobserve/alerts.json` → `agenticos-{cpu,memory,container-ram,disk}-{warning,critical}`
  (mirror the `droplet-*` patterns at `>70%/>90%, 10min` etc., scoped `host_role='agenticos'`).
  The pre-existing generic `droplet-*` USE alerts were scoped to `host_role='odoo-droplet'`
  in the same change so the two hosts sharing the same streams don't blend.
- `docs/RUNBOOKS.md` → `#agenticos-cpu`, `#agenticos-memory`, `#agenticos-container-ram`, `#agenticos-disk`.
- `keep/workflows.yml` — **no change needed**; the new alerts reuse the `keep-webhook`
  destination + the existing `severity` → Discord-channel routing.

## Why push-out (no circular dependency)

AgenticOS is its own failure domain. The collector exports metrics **out** to the obs-droplet
rather than the obs-droplet scraping in. If the obs-droplet dies, AgenticOS keeps running and
DO-native alerts still fire; if AgenticOS dies, the obs-droplet already holds the last window
of metrics. See the ASCII diagram and full rationale in the GOL-51 plan doc / obs spec §5.

## Deploy — GATED on the P0 access grant (SSH to `572389418`)

> Same blocker as P0: this DevOps env has no SSH/DO token, so the final `docker compose up`
> runs on the host once access lands (owner: **CEO - Rick**, see `README.md` §"What I need").
> Everything here is codified + verifiable now; only the apply step needs the box.

On the AgenticOS host (root), after P0.1/P0.2:

```bash
cd infra/agenticos
cp .env.agenticos-otel.example .env.agenticos-otel
$EDITOR .env.agenticos-otel        # OPENOBSERVE_OTLP_BASE + OPENOBSERVE_ROOT_* + COST_ENV=agenticos
docker compose --env-file .env.agenticos-otel -f docker-compose.agenticos-otel.yml up -d
docker logs -f agenticos-otel-collector   # expect no exporter auth/connection errors
```

## Verify end-to-end (smallest proof)

1. **Metrics land:** in obs-droplet OpenObserve, query streams `system_cpu_utilization`,
   `system_memory_utilization`, `container_memory_percent`, `system_filesystem_utilization`
   filtered `host.role = "agenticos"` → rows within ~1 min.
2. **Alerts registered:** re-run `scripts/setup-monitoring.py` (idempotent upsert) against the
   obs-droplet → the 8 `agenticos-*` rules exist.
3. **Alert → Discord:** briefly lower `agenticos-memory-warning` (or `stress-ng --vm 1
   --vm-bytes 80% -t 12m` on the box) → card lands in `#grove-alerts-warning` with the
   `#agenticos-memory` runbook link, then revert.

## Rollback

```bash
docker compose -f docker-compose.agenticos-otel.yml down    # stop shipping; DO-native alerts remain
```
To remove the rules: delete the `agenticos-*` entries from `openobserve/alerts.json` and
re-run `setup-monitoring.py` (upsert-by-name; delete stale rules via the OpenObserve API).

## Caveats / follow-ups

- **Stream/field names + `host_role` predicate are "pending live validation"** (same caveat the
  odoocker collector carries). Confirm on first deploy; adjust exporter + `agenticos-*`
  `stream`/`trigger_condition` if the live names differ.
- **Least privilege (security follow-up):** the collector currently uses the OpenObserve **root**
  basicauth (like the odoocker collector). Provision a scoped ingest-only user on the obs-droplet
  and swap `OPENOBSERVE_ROOT_*` so root creds never live on AgenticOS. Track as a GOL-54 child.
- **obs-droplet must be live + reachable.** If the prod obs-droplet isn't up yet (see
  `docs/MONITORING.md` "what's still aspirational"), point `OPENOBSERVE_OTLP_BASE` at the interim
  OpenObserve or defer the apply until it lands. The config + alerts are ready either way.
