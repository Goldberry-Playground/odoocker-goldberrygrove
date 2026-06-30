#!/usr/bin/env python3
"""
Grove synthetic Tier-1 runner.

Runs the Hurl journeys in ./journeys against the live Odoo grove_headless API,
once per tenant (plus the shared health journey), then ships the results to
OpenObserve as OTLP metrics:

    synthetic_journey_success      gauge 1/0   {journey, tenant, tier, env}
    synthetic_journey_duration_ms  gauge ms    {journey, tenant, tier, env}

Invoked every minute by supercronic (see ./crontab). Pass/fail is the Hurl
`--test` exit code; duration is the wall-clock of the Hurl run (good enough for
latency alerting at this tier). The process always exits 0 — failures are
reported as metrics, not as a crashed cron job (which supercronic would only
surface in logs).

Mirrors scripts/setup-monitoring.py: stdlib only, all logs to stderr.

Env contract (see .env.monitoring.example):
    SYNTHETIC_ODOO_BASE            default http://odoo:8069
    SYNTHETIC_TENANTS             default goldberry,ggg,nursery
    SYNTHETIC_ENV                 default local   (env label on every metric)
    OPENOBSERVE_OTLP_METRICS_URL  default http://openobserve:5080/api/default/v1/metrics
    OPENOBSERVE_ROOT_EMAIL        basic-auth user for OpenObserve ingest
    OPENOBSERVE_ROOT_PASSWORD     basic-auth pass
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request

SCRIPT_DIR = Path(__file__).resolve().parent
JOURNEY_DIR = SCRIPT_DIR / "journeys"

# Per-tenant journeys (run once for each tenant) and shared journeys (run once,
# tagged tenant="shared"). Keep names stable — alert rules query them by value.
TENANT_JOURNEYS = [("catalog", "catalog.hurl"), ("cart-flow", "cart-flow.hurl")]
SHARED_JOURNEYS = [("health", "health.hurl")]

CONNECT_TIMEOUT_S = "5"
MAX_TIME_S = "20"


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def run_journey(journey_file: str, *, tenant: str, odoo_base: str) -> tuple[int, float]:
    """Run one Hurl journey. Returns (success 1/0, duration_ms)."""
    path = JOURNEY_DIR / journey_file
    cmd = [
        "hurl",
        "--test",
        "--connect-timeout",
        CONNECT_TIMEOUT_S,
        "--max-time",
        MAX_TIME_S,
        "--variable",
        f"odoo_base={odoo_base}",
        "--variable",
        f"tenant={tenant}",
        str(path),
    ]
    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=float(MAX_TIME_S) + 10)
        rc = proc.returncode
        if rc != 0:
            _log(f"    ✗ {journey_file} [{tenant}] rc={rc}\n{proc.stderr.strip()[:500]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        rc = 1
        _log(f"    ✗ {journey_file} [{tenant}] runner error: {exc}")
    duration_ms = (time.perf_counter() - start) * 1000.0
    return (1 if rc == 0 else 0), duration_ms


def build_otlp_metrics(results: list[dict], now_ns: int, env_label: str) -> dict:
    """Build an OTLP/HTTP JSON metrics payload from journey results.

    `results` is a list of {journey, tenant, success, duration_ms}. Emits two
    gauges sharing the same attribute set per data point. Pure function — unit
    tested in test_run.py.
    """
    success_points = []
    duration_points = []
    for r in results:
        attrs = [
            {"key": "journey", "value": {"stringValue": r["journey"]}},
            {"key": "tenant", "value": {"stringValue": r["tenant"]}},
            {"key": "tier", "value": {"stringValue": "api"}},
            {"key": "env", "value": {"stringValue": env_label}},
        ]
        success_points.append({"asInt": str(int(r["success"])), "timeUnixNano": str(now_ns), "attributes": attrs})
        duration_points.append({"asDouble": float(r["duration_ms"]), "timeUnixNano": str(now_ns), "attributes": attrs})
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "grove-synthetic"}}]},
                "scopeMetrics": [
                    {
                        "scope": {"name": "grove.synthetic", "version": "1"},
                        "metrics": [
                            {
                                "name": "synthetic_journey_success",
                                "unit": "1",
                                "gauge": {"dataPoints": success_points},
                            },
                            {
                                "name": "synthetic_journey_duration_ms",
                                "unit": "ms",
                                "gauge": {"dataPoints": duration_points},
                            },
                        ],
                    }
                ],
            }
        ]
    }


def ship(payload: dict) -> None:
    url = _env(
        "OPENOBSERVE_OTLP_METRICS_URL",
        "http://openobserve:5080/api/default/v1/metrics",
    )
    email = _env("OPENOBSERVE_ROOT_EMAIL")
    password = _env("OPENOBSERVE_ROOT_PASSWORD")
    headers = {"Content-Type": "application/json"}
    if email and password:
        token = base64.b64encode(f"{email}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    points = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"]
    req = request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with request.urlopen(req, timeout=10) as resp:
            _log(f"  shipped {len(points)} results → OpenObserve (HTTP {resp.status})")
    except urlerror.HTTPError as exc:
        _log(f"  ⚠ ship HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urlerror.URLError as exc:
        _log(f"  ✗ ship network error: {exc.reason}")


def main() -> None:
    odoo_base = _env("SYNTHETIC_ODOO_BASE", "http://odoo:8069")
    tenants = [t.strip() for t in _env("SYNTHETIC_TENANTS", "goldberry,ggg,nursery").split(",") if t.strip()]
    env_label = _env("SYNTHETIC_ENV", "local")
    _log(f"=== synthetic run: tenants={tenants} odoo={odoo_base} env={env_label} ===")

    results: list[dict] = []
    for journey_name, journey_file in SHARED_JOURNEYS:
        success, dur = run_journey(journey_file, tenant="shared", odoo_base=odoo_base)
        results.append({"journey": journey_name, "tenant": "shared", "success": success, "duration_ms": dur})
    for tenant in tenants:
        for journey_name, journey_file in TENANT_JOURNEYS:
            success, dur = run_journey(journey_file, tenant=tenant, odoo_base=odoo_base)
            results.append({"journey": journey_name, "tenant": tenant, "success": success, "duration_ms": dur})

    passed = sum(r["success"] for r in results)
    _log(f"  {passed}/{len(results)} journeys passed")
    ship(build_otlp_metrics(results, time.time_ns(), env_label))


if __name__ == "__main__":
    main()
