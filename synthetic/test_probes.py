#!/usr/bin/env python3
"""Unit tests for the synthetic uptime/ssl probe builders.

Runs without a network, OpenObserve, or real targets — verifies the pure logic:
    python3 synthetic/test_probes.py
"""

from __future__ import annotations

import sys

import probes


def test_load_monitors_skips_comment_header() -> None:
    mons = probes.load_monitors()
    # every returned entry is a real probe (has name + type), never the header
    assert mons, "expected monitors from openobserve/monitors.json"
    assert all(m.get("name") and m.get("type") for m in mons)
    types = {m["type"] for m in mons}
    assert {"http", "tcp", "ssl"} <= types, types


def test_tags_to_attrs() -> None:
    mon = {"tags": ["tenant:hub", "tier:frontend", "route:root"]}
    assert probes.tags_to_attrs(mon) == {"tenant": "hub", "tier": "frontend", "route": "root"}


def test_expected_codes() -> None:
    assert probes.expected_codes({"expected_status": 200}) == {200}
    assert probes.expected_codes({"expected_status_in": [200, 303]}) == {200, 303}
    assert probes.expected_codes({}) == {200}  # default


def test_env_override(monkeypatch=None) -> None:
    import os

    mon = {"name": "hub-root", "url": "http://host.docker.internal:3000/"}
    assert probes.monitor_url(mon) == "http://host.docker.internal:3000/"
    os.environ["SYNTHETIC_MONITOR_URL_HUB_ROOT"] = "https://gatheringatthegrove.com/"
    try:
        assert probes.monitor_url(mon) == "https://gatheringatthegrove.com/"
    finally:
        del os.environ["SYNTHETIC_MONITOR_URL_HUB_ROOT"]


def test_port_override() -> None:
    import os

    mon = {"name": "postgres-tcp", "host": "postgres", "port": 5432}
    assert probes.monitor_port(mon, int(mon["port"])) == 5432  # file default
    os.environ["SYNTHETIC_MONITOR_PORT_POSTGRES_TCP"] = "25060"  # managed PG
    try:
        assert probes.monitor_port(mon, int(mon["port"])) == 25060
    finally:
        del os.environ["SYNTHETIC_MONITOR_PORT_POSTGRES_TCP"]


def test_probe_sends_identifiable_user_agent() -> None:
    # CF Bot Fight Mode 403s the default urllib UA on proxied targets (GOL-334).
    assert "GroveSyntheticMonitor" in probes.USER_AGENT


def test_build_uptime_otlp_shape() -> None:
    uptime = [
        {"success": 1, "duration_ms": 12.0, "target": "hub-root", "tenant": "hub", "tier": "frontend", "route": "root", "service": ""},
        {"success": 0, "duration_ms": 30.0, "target": "postgres-tcp", "tenant": "shared", "tier": "backend", "service": "postgres", "route": ""},
    ]
    ssl_res = [
        {"days": 42, "host": "goldberrygrove.farm", "target": "ssl-goldberrygrove-farm", "tenant": "goldberry", "tier": "cert", "service": "", "route": ""},
        {"days": None, "host": "unreachable.example", "target": "ssl-broken", "tenant": "x"},  # handshake failed -> dropped
    ]
    payload = probes.build_uptime_otlp(uptime, ssl_res, now_ns=1_700_000_000_000_000_000, env_label="qa")
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    names = [m["name"] for m in metrics]
    assert names == ["synthetic_uptime", "synthetic_ssl_days"], names

    up = next(m for m in metrics if m["name"] == "synthetic_uptime")["gauge"]["dataPoints"]
    assert len(up) == 2
    assert up[0]["asInt"] == "1" and up[1]["asInt"] == "0"
    # success is present as an explicit int column so the alert's `success=0` filter resolves
    a0 = {a["key"]: a["value"] for a in up[0]["attributes"]}
    assert a0["success"] == {"intValue": "1"}
    assert a0["target"] == {"stringValue": "hub-root"}
    # empty tags (service/route on hub) are omitted, not blank columns
    assert "service" not in a0

    ssl_pts = next(m for m in metrics if m["name"] == "synthetic_ssl_days")["gauge"]["dataPoints"]
    assert len(ssl_pts) == 1, "unreachable cert must not emit a fake data point"
    s0 = {a["key"]: a["value"] for a in ssl_pts[0]["attributes"]}
    assert ssl_pts[0]["asDouble"] == 42.0
    assert s0["days_until_expiry"] == {"intValue": "42"}
    assert s0["host"] == {"stringValue": "goldberrygrove.farm"}


def test_build_uptime_otlp_no_certs_omits_ssl_metric() -> None:
    payload = probes.build_uptime_otlp([{"success": 1, "target": "x"}], [], now_ns=1, env_label="local")
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert [m["name"] for m in metrics] == ["synthetic_uptime"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
    sys.exit(0)
