# Grove Observability — Design Spec

**Status:** Design approved (brainstorm + grill complete 2026-06-26) — pending written-spec review
**Scope:** Deepen the shipped OpenObserve + Keep stack (GATH-44) with synthetic journeys, RUM, APM/infra, resilient prod deployment, and ADR-004-governed promotion.
**Related:** ADR-004 (QA promotion model), ADR-005 (cert resilience), ADR-006 (hub subdomain), ADR-007 (Level 3 App Platform migration), `docs/MONITORING.md`, `docker-compose.monitoring.yml`.

---

## 0. Reframe & platform decision

This is **not greenfield.** GATH-44 already shipped an all-in-one open-source observability platform: **OpenObserve** (single binary — synthetic monitors + logs/metrics/traces + alerting + dashboards, Parquet on MinIO, AGPL-3.0) **+ Keep** (alert routing → Discord). It deliberately replaced the vault's original LGTM + Uptime Kuma + Plausible plan on cost grounds.

**Decision:** Deepen OpenObserve + Keep across three functional axes (synthetic, RUM, APM/infra), deploy it resiliently on the ADR-007 topology, and govern it with the ADR-004 promotion model. Everything new is an *emitter into OpenObserve*; Keep's downstream Discord routing is untouched.

**Open conflict to record as ADR-008:** ADR-004's "Observability" section still lists Loki + Prometheus/Grafana + Sentry (its deferred Phase 11). The shipped GATH-44 stack (OpenObserve + Keep) supersedes that. ADR-008 should record the supersession so the repo has one observability source of truth.

---

## 1. Synthetic tests (cart / blog / mobile)

Two tiers, both reusing the existing CI suites but run continuously against **production** from an external vantage.

### Tier 1 — Hurl API journeys (~60s)
Runner: **supercronic** container (not a bare shell loop — crashing jobs surface in `docker logs`, no silent death). Results extracted to pass/fail (1/0) + latency via a **jq transform** and POSTed as **OTLP metrics** to OpenObserve (consistent with Beyla RED + built-in monitor metrics — one unified metrics layer for all alerting).

Per tenant (`goldberry / ggg / nursery`) + `hub`/`shared`:

| Journey | Asserts | Catches |
|---|---|---|
| `products-list` | 200, array non-empty | Catalog/DB outage, empty company scope |
| `product-detail` | 200, has price | Pricing/CoA breakage |
| `cart-add → cart-get` | POST then GET reflects qty | BFF↔Odoo session break |
| `checkout-canary` | POST order for **$0 SYNTHETIC-CANARY SKU + test partner**, assert `order_id`+`access_token`, **auto-cancel** | The money path incl. `access_token` gate |
| `ghost-content` | Ghost Content API 200, ≥1 post | Ghost down / key rotation |
| `health` | `/grove/api/v1/health` 200 | Module not loaded |

**Canary seed:** the `$0 SYNTHETIC-CANARY` product + test partner are upserted per Odoo company by **`setup-monitoring.py`** (≈30 lines XML-RPC) before monitors upload. `make monitoring-up` depends on `make monitoring-setup`, so the runner never fires without seed records. Canary orders auto-cancel in-journey — exercises the write path without polluting books.

### Tier 2 — Playwright browser journeys (~10–15min)
Runner: **GitHub Actions cron** (`schedule`), off-droplet — zero RAM on the app/obs droplets, reuses existing CI Playwright infra. Ships OTLP metrics to OpenObserve via a **Cloudflare-WAF-protected ingest subdomain** (Bearer-token rule), same Cloudflare already fronting all domains.

Per tenant:

| Journey | Asserts | Catches (that Tier-1 can't) |
|---|---|---|
| `shop-to-cart` | `/shop` → click → Add → badge increments, `/cart` shows line | **Client-side localStorage cart** |
| `blog-render` | `/blog` ≥1 card → open post → body present | Ghost webhook / ISR drift |
| `checkout-render` | `/checkout` form fields present (no real submit) | Broken checkout UI |
| `mobile-shop-to-cart` | same journey at **390×844**, no overflow, tap targets ≥44px | **Mobile layout regressions** |
| `hub-directory` | hub → all 3 tenant links resolve 200 | Cross-tenant link rot |
| `console-clean` (rider) | no console errors / failed requests | Silent client JS breakage |

---

## 2. RUM (engineering alerting + marketing)

`@grove/analytics` (today a `console.warn` stub) becomes a lazy **dual-writer**: OpenObserve RUM (alertable) + Plausible (marketing). Wiring lives at the package boundary — zero app changes.

### Web Vitals alerts
| Metric | Warning (p75 sustained) | Critical (p75 sustained) |
|---|---|---|
| LCP | >3s / 10min | >4s / 5min |
| INP | >400ms / 10min | — |
| CLS | >0.15 | >0.25 |
| TTFB | >1.5s | >2.5s |

(TTFB is the BFF diagnostic — high TTFB points into Odoo/Ghost without needing a trace.)

### Funnel events (→ OpenObserve + Plausible)
`product_viewed`, `add_to_cart`, `cart_viewed`, `checkout_started`, `checkout_completed` (with **`order_id_hash`** — SHA-256, never the enumerable raw ID), `checkout_error` (client-side BFF non-2xx), `blog_post_viewed`.

### RUM alert rules
| Alert | Condition | Severity |
|---|---|---|
| `rum-error-spike` | JS error rate >3× 5-min baseline | critical |
| `rum-checkout-error-spike` | `checkout_error` >5% of `checkout_started` / 10min | critical |
| `rum-funnel-drop` | conversion <50% / 30min, **min 10 events** | warning |
| `rum-lcp-*` / `rum-ttfb-*` | per table above | warning/critical |

Relative baselines + min-sample guards prevent low-traffic false pages.

### Session replay
**Masked, on error/slow (LCP>4s) triggers**; all checkout form fields `data-record-ignore`'d → DOM structure captured, no PII, no consent banner.

### Plausible
**Self-hosted CE, PRODUCTION ONLY** (needs Clickhouse ~1GB → fits prod's 8GB droplet; QA's dev-tier RAM is too tight). QA/preview skip Plausible entirely. No alerting path touches Plausible.

---

## 3. APM + Infrastructure (eBPF-first)

### Next.js OTel
`instrumentation.ts` per app: `ParentBasedSampler(TraceIdRatioBased(0.2))`, **100% on `/api/checkout`**. Resource attrs: `service.name` (tenant), `service.version` (git SHA), `deployment.environment`. `traceparent` auto-propagates BFF→Odoo so one `trace_id` spans Next.js → Odoo WSGI → Postgres. Works on App Platform (just app code + OTLP env var).

### Beyla eBPF — Odoo only
`privileged: true` + `pid: host` sidecar on the Odoo droplet. RED metrics (rate/error/p50/p95/p99) per `/grove/api/v1/*` endpoint, zero Odoo code, survives Odoo upgrades. **Single sidecar** — the original "one vs two" fork dissolved because ADR-007's Managed Postgres has no kernel access, so Beyla targets only Odoo.

### OTel Collector — USE/infra
`hostmetrics` + `docker_stats` (Odoo droplet host + odoo/caddy containers) + **`postgresql` receiver** (client connection to Managed PG `pg_stat_*` via `otel_monitor`/`pg_monitor`). Pipeline: receivers → batch/resource/filter/transform → OTLP → OpenObserve.

### RED + USE alert thresholds (new rules in `alerts.json`)
| Alert | Condition | Sev |
|---|---|---|
| `nextjs-checkout-latency-{warning,critical}` | p95 `/api/checkout` >2s / >5s, 5min | warn/crit |
| `nextjs-cart-latency-warning` | p95 `/api/cart` >1.5s | warn |
| `odoo-orders-latency-{warning,critical}` | p95 `/orders` >3s / >8s | warn/crit |
| `odoo-products-latency-warning` | p95 `/products` >1s | warn |
| `nextjs-5xx-rate-{warning,critical}` | >2% / >10%, 5min | warn/crit |
| `droplet-cpu-{warning,critical}` | >70% / >90%, 10min | warn/crit |
| `container-ram-{warning,critical}` | >80% / >95%, 5min | warn/crit |
| `disk-data-{warning,critical}` | `/data` >75% / >90% | warn/crit |
| `postgres-connections-{warning,critical}` | >70% / >90% of max | warn/crit |

Conservative windows/percentages — at solo-dev tier, alert fatigue is the bigger risk. Ghost: availability-only (no latency alert). Python OTel SDK in `grove_headless`: **deferred, on-demand** (added only when Beyla surfaces an Odoo bottleneck it can't localize).

### Compose
New `docker-compose.apm.yml` (Beyla + Collector), separate from `docker-compose.monitoring.yml` so the eBPF/privileged layer is independently deployable.

---

## 4. Deployment topology (ADR-007 Level 3)

| Plane | Runs | TLS / data |
|---|---|---|
| **App Platform** | hub, goldberry, ggg, nursery (4 Next.js apps) | Managed TLS, per-app isolation |
| **Tiny Odoo droplet** | Odoo + Caddy (1 cert/host) + Beyla + OTel Collector | Caddy/LE, persistent volume (ADR-005) |
| **DO Managed Postgres** | shared instance, per-company data | Backups + PITR, private network |
| **Observability droplet (NEW, separate)** | OpenObserve + Keep + Plausible(prod) | Own failure domain |
| **AgenticOS droplet (existing)** | Self-hosted Healthchecks (dead-man's-switch) | Separate failure domain |

QA built first → validate ~2 weeks → prod replicated **same shape** (ADR-007 D1). The observability + AgenticOS droplets are intentionally separate from the app plane so monitoring outlives an app-plane outage.

---

## 5. Resilience — three independent alert paths

No single failure that blinds you:

1. **OpenObserve + Keep** (obs droplet) → Discord — the primary single pane.
2. **DO-native alerts** — App Platform `DEPLOYMENT_FAILED`/`DOMAIN_FAILED` (already in `infra/do/*.yaml`) + Managed PG/Redis alert policies → Discord. Fires even if OpenObserve is down.
3. **GitHub Actions external synthetic cron** — depends on zero droplets; catches total outage.

**Dead-man's-switch (the watcher-of-the-watcher):** **self-hosted Healthchecks** (Python/Django/Postgres, BSD-3, native Discord) on the **AgenticOS droplet** — a separate failure domain at $0 incremental. OpenObserve/Keep curl a ping URL every 60s; if pings stop, Healthchecks fires Discord independently. The agent fleet (Paperclip routines, Dev Agent heartbeat, qa-deploy chain) also pings it → unified heartbeat for prod monitoring *and* the execution brain.

**Hardening against the AI noisy-neighbor** (agent storage/RAM growth on AgenticOS): dedicated **DO block volume** for Healthchecks' Postgres (ADR-005 PR-A pattern, ~$1/mo), Docker **`mem_limit`**, and AgenticOS disk/RAM metrics shipped to the **Grove obs-droplet** OpenObserve for early warning (independent host → no circular dependency). Explicitly NOT pointing Healthchecks' DB at Grove prod Managed PG (would re-couple failure domains).

**DO-API metrics bridge:** a ~50-line poller (GH Actions cron or obs-droplet container) pulls App Platform (`/v2/monitoring/metrics/apps/`: CPU/mem/restart + request-rate/p95) + Managed PG + Spaces metrics → OTLP → OpenObserve, closing the unification gap (App Platform/Managed services have no OTel-native export).

**Residual (named, accepted):** if AgenticOS + Grove droplets share a DO region, a region-wide outage takes both. Accepted at farm/solo-dev tier; cross-provider free VM is the upgrade path if ever needed.

---

## 6. Automation & promotion (ADR-004 Option A)

Observability folds into the existing promotion backbone — **unified control plane, isolated failure planes:**

- Obs droplet + Healthchecks volume + Beyla/Collector = **Terraform resources** in each env dir (`qa-app-platform/`, then `production/`).
- OpenObserve/Keep/Beyla/Collector **image tags pinned in `release-manifest.yaml`** alongside the 4 frontend SHAs.
- Config-as-code (`monitors.json`, `alerts.json`, Keep `workflows.yml`/`providers.yml`) applied by the **idempotent `setup-monitoring.py`** as a post-deploy step in `qa-deploy.yml`.
- **Promotion = `qa→main` merge** — a monitor/threshold change is reviewed, SHA-pinned, smoke-gated, and bisectable exactly like app code. Manifest git history = observability deploy timeline.

---

## 7. QA↔Prod drift & triage impact

ADR-004's SHA-pinned manifest makes **code identical** QA→prod; ADR-007 D1 makes **topology identical**. Residual, documented drift that can still break a clean-QA promotion:

| Drift | Source | Triage risk |
|---|---|---|
| Hub apex vs `hub.qa.*` subdomain | ADR-006 | Different cert/routing path + env-aware `sibling-sites.ts` branch in prod |
| Instance/PG tier sizing | ADR-007 D6 | OOM / pool / slow-query bugs surface at only one tier |
| Data volume & shape | ADR-004 (QA wipes + seed) | Real-data-only query/migration regressions |
| KeyDB/MinIO/Ghost maybe absent in QA | ADR-007 open items | BFF KeyDB caching path untested before prod |

**Mitigation = this spec.** Prod-equal observability (same RED/USE/RUM alerts) makes prod-only divergences instantly visible; the SHA manifest makes them trivially bisectable. This is the argument for **not** deferring observability to ADR-007 Phase 11.

---

## 8. Cost

| Item | Cost |
|---|---|
| OpenObserve, Keep, Hurl, Playwright, Beyla, OTel Collector, Healthchecks | $0 (open-source, self/CI-hosted) |
| Plausible CE (prod only) | $0 (self-hosted on 8GB prod droplet) |
| Observability droplet | ~$12–24/mo |
| Healthchecks block volume (AgenticOS) | ~$1/mo |
| Discord routing | $0 |

Everything else (App Platform, Managed PG, Spaces, Odoo droplet) is ADR-007 spend, not observability spend.

---

## 9. Open follow-ups

- **ADR-008** — record OpenObserve+Keep supersedes ADR-004's Loki/Prom/Grafana/Sentry observability section.
- DO-metrics bridge: confirm poll cadence + which DO metrics map to which OpenObserve streams.
- Decide KeyDB (Managed Redis vs droplet) + MinIO (Spaces vs droplet) placement (ADR-007 open items) — affects what infra the Collector targets.
- `status.gatheringatthegrove.com` public status page (OpenObserve dashboard) — lands with prod DNS.
