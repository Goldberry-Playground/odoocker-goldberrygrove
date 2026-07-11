#!/usr/bin/env python3
"""
Grove synthetic availability + SSL probes (Tier-1 uptime).

OpenObserve v0.91.1 has NO /synthetic endpoint, so the availability targets in
`openobserve/monitors.json` (storefront roots/shop/blog, hub, Odoo, Ghost admin,
Postgres TCP, SSL certs) are NOT OpenObserve objects — this module makes the
synthetic-runner probe them directly and ship two OTLP metric streams:

    synthetic_uptime     gauge 1/0   {target, tenant, tier, service, route, env, success}
    synthetic_ssl_days   gauge days  {target, host, tenant, env, days_until_expiry}

These are the streams the availability alerts (frontend-down / odoo-down /
ghost-down / postgres-down) and ssl-expiring alert fire on (openobserve/alerts.json,
GOL-278). Emitting them is what makes those alert streams exist (GOL-280); until
a source ships data, OpenObserve won't create the stream and the v2 alert POST
400s on the missing stream.

`monitors.json` is the single source of truth for targets — this module reads it,
so adding/removing a probe is a config change, not a code change. Local topology
(host.docker.internal ports, service-name DNS) is the file default; each target's
URL/host can be overridden per-env without editing the file:

    SYNTHETIC_MONITOR_URL_<NAME>   e.g. SYNTHETIC_MONITOR_URL_HUB_ROOT
    SYNTHETIC_MONITOR_HOST_<NAME>  e.g. SYNTHETIC_MONITOR_HOST_POSTGRES_TCP

where <NAME> is the monitor name upper-cased with '-' -> '_'.

Field-name note (pending live validation, GOL-280): the alert conditions filter
`success` (uptime) and `days_until_expiry` (ssl) as COLUMNS. We therefore emit
those numbers BOTH as the gauge value AND as an explicit numeric OTLP attribute
so the column exists regardless of how OpenObserve names the bare gauge value on
ingest — confirm on first real traffic and drop the redundant one.

stdlib only, all logs to stderr, mirrors synthetic/run.py conventions. Pure
builders (build_uptime_otlp, tags_to_attrs, ...) are unit-tested in
test_probes.py; the probe/ship IO is exercised live from the runner.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MONITORS_PATH = REPO_ROOT / "openobserve" / "monitors.json"

DEFAULT_HTTP_TIMEOUT_S = 10
DEFAULT_TCP_TIMEOUT_S = 5
DEFAULT_SSL_TIMEOUT_S = 10


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# ── config (monitors.json is the source of truth) ─────────────────────────────
def load_monitors(path: Path | str = MONITORS_PATH) -> list[dict]:
    """Real monitor entries (skip the pure-comment header). An entry is real if
    it has both a `name` and a `type` (the ssl block carries a `_comment` too)."""
    data = json.loads(Path(path).read_text())
    return [m for m in data.get("monitors", []) if m.get("name") and m.get("type")]


def env_key(name: str) -> str:
    return name.upper().replace("-", "_")


def monitor_url(mon: dict) -> str:
    return _env(f"SYNTHETIC_MONITOR_URL_{env_key(mon['name'])}") or mon.get("url", "")


def monitor_host(mon: dict) -> str:
    return _env(f"SYNTHETIC_MONITOR_HOST_{env_key(mon['name'])}") or mon.get("host", "")


def tags_to_attrs(mon: dict) -> dict[str, str]:
    """`["tenant:hub","tier:frontend","route:root"]` -> {tenant,tier,route,...}."""
    out: dict[str, str] = {}
    for t in mon.get("tags", []):
        if ":" in t:
            k, v = t.split(":", 1)
            out[k] = v
    return out


def expected_codes(mon: dict) -> set[int]:
    if mon.get("expected_status_in"):
        return {int(c) for c in mon["expected_status_in"]}
    return {int(mon.get("expected_status", 200))}


# ── probes (return a result dict per target) ──────────────────────────────────
def probe_http(mon: dict) -> dict:
    url = monitor_url(mon)
    timeout = float(mon.get("timeout_seconds", DEFAULT_HTTP_TIMEOUT_S))
    codes = expected_codes(mon)
    keyword = mon.get("expected_keyword")
    start = time.perf_counter()
    ok = False
    try:
        req = request.Request(url, method=mon.get("method", "GET"))
        with request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            body = resp.read().decode("utf-8", "replace") if keyword else ""
        ok = code in codes and (keyword in body if keyword else True)
    except urlerror.HTTPError as e:
        # 401/403/etc are "expected up" for some targets (e.g. Ghost admin 401).
        # Keyword can't be asserted on an error body, so code-match is enough.
        ok = e.code in codes
    except Exception as exc:  # timeout, DNS, conn refused -> down
        _log(f"    ✗ http {mon['name']} unreachable: {exc}")
    dur = (time.perf_counter() - start) * 1000.0
    return {"success": 1 if ok else 0, "duration_ms": dur, **_identity(mon)}


def probe_tcp(mon: dict) -> dict:
    host, port = monitor_host(mon), int(mon["port"])
    timeout = float(mon.get("timeout_seconds", DEFAULT_TCP_TIMEOUT_S))
    start = time.perf_counter()
    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError as exc:
        _log(f"    ✗ tcp {mon['name']} {host}:{port} down: {exc}")
    dur = (time.perf_counter() - start) * 1000.0
    return {"success": 1 if ok else 0, "duration_ms": dur, **_identity(mon)}


def probe_ssl(mon: dict) -> dict:
    """Returns identity + `days` (int days until cert expiry) or None if the TLS
    handshake failed. `reachable` mirrors success for the ssl target itself."""
    host, port = monitor_host(mon), int(mon.get("port", 443))
    timeout = float(mon.get("timeout_seconds", DEFAULT_SSL_TIMEOUT_S))
    ctx = ssl.create_default_context()
    days: int | None = None
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        # notAfter e.g. 'Jun  1 12:00:00 2026 GMT'
        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (expiry - datetime.now(timezone.utc)).days
    except Exception as exc:
        _log(f"    ✗ ssl {mon['name']} {host}: {exc}")
    return {"days": days, "host": host, **_identity(mon)}


def _identity(mon: dict) -> dict:
    attrs = tags_to_attrs(mon)
    return {
        "target": mon["name"],
        "tenant": attrs.get("tenant", ""),
        "tier": attrs.get("tier", ""),
        "service": attrs.get("service", ""),
        "route": attrs.get("route", ""),
    }


PROBERS = {"http": probe_http, "tcp": probe_tcp, "ssl": probe_ssl}


def run_probes(monitors: list[dict]) -> tuple[list[dict], list[dict]]:
    """Dispatch each monitor by type. Returns (uptime_results, ssl_results)."""
    uptime: list[dict] = []
    ssl_res: list[dict] = []
    for mon in monitors:
        prober = PROBERS.get(mon["type"])
        if prober is None:
            _log(f"    ⚠ unknown monitor type {mon['type']} for {mon['name']} — skipping")
            continue
        result = prober(mon)
        (ssl_res if mon["type"] == "ssl" else uptime).append(result)
    return uptime, ssl_res


# ── OTLP builder (pure — unit tested) ─────────────────────────────────────────
def _kv(k: str, v: str) -> dict:
    return {"key": k, "value": {"stringValue": str(v)}}


def _kv_int(k: str, v: int) -> dict:
    return {"key": k, "value": {"intValue": str(int(v))}}


def build_uptime_otlp(uptime: list[dict], ssl_res: list[dict], now_ns: int, env_label: str) -> dict:
    """OTLP/HTTP JSON payload with synthetic_uptime (+ synthetic_ssl_days when any
    cert resolved). Pure function — no I/O."""
    up_points = []
    for r in uptime:
        attrs = [_kv("target", r["target"]), _kv("env", env_label), _kv_int("success", r["success"])]
        for k in ("tenant", "tier", "service", "route"):
            if r.get(k):
                attrs.append(_kv(k, r[k]))
        up_points.append({"asInt": str(int(r["success"])), "timeUnixNano": str(now_ns), "attributes": attrs})

    metrics: list[dict] = [{"name": "synthetic_uptime", "unit": "1", "gauge": {"dataPoints": up_points}}]

    ssl_points = []
    for r in ssl_res:
        if r.get("days") is None:  # handshake failed — emit no fake value
            continue
        attrs = [_kv("target", r["target"]), _kv("host", r["host"]), _kv("env", env_label),
                 _kv_int("days_until_expiry", int(r["days"]))]
        if r.get("tenant"):
            attrs.append(_kv("tenant", r["tenant"]))
        ssl_points.append({"asDouble": float(r["days"]), "timeUnixNano": str(now_ns), "attributes": attrs})
    if ssl_points:
        metrics.append({"name": "synthetic_ssl_days", "unit": "d", "gauge": {"dataPoints": ssl_points}})

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [_kv("service.name", "grove-synthetic")]},
                "scopeMetrics": [{"scope": {"name": "grove.synthetic.probes", "version": "1"}, "metrics": metrics}],
            }
        ]
    }


def ship(payload: dict) -> bool:
    """POST the OTLP payload to OpenObserve. Returns True on 2xx."""
    url = _env("OPENOBSERVE_OTLP_METRICS_URL", "http://openobserve:5080/api/default/v1/metrics")
    email, password = _env("OPENOBSERVE_ROOT_EMAIL"), _env("OPENOBSERVE_ROOT_PASSWORD")
    headers = {"Content-Type": "application/json"}
    if email and password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()
    n = sum(len(m["gauge"]["dataPoints"]) for m in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"])
    req = request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with request.urlopen(req, timeout=10) as resp:
            _log(f"  shipped {n} uptime/ssl points → OpenObserve (HTTP {resp.status})")
            return 200 <= resp.status < 300
    except urlerror.HTTPError as exc:
        _log(f"  ⚠ uptime ship HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urlerror.URLError as exc:
        _log(f"  ✗ uptime ship network error: {exc.reason}")
    return False


def emit(env_label: str, monitors_path: Path | str = MONITORS_PATH) -> None:
    """Load monitors, probe every target, ship the two streams. Gated by the
    caller (SYNTHETIC_UPTIME_ENABLED). Never raises — a probe failure is data."""
    monitors = load_monitors(monitors_path)
    _log(f"  probing {len(monitors)} availability/ssl targets...")
    uptime, ssl_res = run_probes(monitors)
    up = sum(r["success"] for r in uptime)
    certs = sum(1 for r in ssl_res if r.get("days") is not None)
    _log(f"  uptime {up}/{len(uptime)} targets up; {certs}/{len(ssl_res)} certs resolved")
    ship(build_uptime_otlp(uptime, ssl_res, time.time_ns(), env_label))


if __name__ == "__main__":  # manual: python3 synthetic/probes.py
    emit(_env("SYNTHETIC_ENV", "local"))
