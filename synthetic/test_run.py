#!/usr/bin/env python3
"""Unit tests for the synthetic runner's OTLP metric builder and cart cookie jars.

Runs without Hurl, OpenObserve, or a network — verifies the pure logic
locally:  python3 synthetic/test_run.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import run


def test_otlp_shape() -> None:
    results = [
        {"journey": "health", "tenant": "shared", "success": 1, "duration_ms": 12.5},
        {"journey": "cart-flow", "tenant": "goldberry", "success": 0, "duration_ms": 2003.1},
    ]
    payload = run.build_otlp_metrics(results, now_ns=1_700_000_000_000_000_000, env_label="local")

    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    names = {m["name"] for m in metrics}
    assert names == {"synthetic_journey_success", "synthetic_journey_duration_ms"}, names

    success = next(m for m in metrics if m["name"] == "synthetic_journey_success")
    points = success["gauge"]["dataPoints"]
    assert len(points) == 2, len(points)
    # success is an int gauge expressed as a string per OTLP/HTTP JSON
    assert points[0]["asInt"] == "1" and points[1]["asInt"] == "0"

    duration = next(m for m in metrics if m["name"] == "synthetic_journey_duration_ms")
    dpoints = duration["gauge"]["dataPoints"]
    assert dpoints[0]["asDouble"] == 12.5

    # every data point carries the four expected attributes
    attr_keys = {a["key"] for a in points[0]["attributes"]}
    assert attr_keys == {"journey", "tenant", "tier", "env"}, attr_keys
    tier = next(a for a in points[0]["attributes"] if a["key"] == "tier")
    assert tier["value"]["stringValue"] == "api"


def test_empty_results() -> None:
    payload = run.build_otlp_metrics([], now_ns=1, env_label="local")
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert all(m["gauge"]["dataPoints"] == [] for m in metrics)


def test_cart_cookie_jar_per_tenant_per_day() -> None:
    day1 = datetime(2026, 11, 2, 23, 59, tzinfo=timezone.utc)
    day2 = datetime(2026, 11, 3, 0, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"  # created on demand
        nursery = run.cart_cookie_jar("nursery", state, day1)
        farm = run.cart_cookie_jar("goldberry", state, day1)
        # Same tenant + same day -> same jar, so the Odoo session (and cart) is reused.
        assert run.cart_cookie_jar("nursery", state, day1) == nursery
        # Tenants share one Odoo host, so their sessions must never share a jar.
        assert nursery != farm
        nursery.write_text("cookie")
        farm.write_text("cookie")

        rolled = run.cart_cookie_jar("nursery", state, day2)
        assert rolled != nursery
        # The new day prunes that tenant's stale jar but leaves other tenants alone.
        assert not nursery.exists()
        assert farm.exists()


def test_run_journey_passes_cookie_jar() -> None:
    calls = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Proc()

    real_run = run.subprocess.run
    run.subprocess.run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "jar.cookies"
            # First run of the day: no jar yet -> write it, don't read it (Hurl
            # errors on a missing --cookie file).
            run.run_journey("cart-flow.hurl", tenant="nursery", odoo_base="http://x", cookie_jar=jar)
            assert "--cookie-jar" in calls[-1] and "--cookie" not in calls[-1]
            jar.write_text("cookie")
            run.run_journey("cart-flow.hurl", tenant="nursery", odoo_base="http://x", cookie_jar=jar)
            assert calls[-1][calls[-1].index("--cookie") + 1] == str(jar)
            # Journeys without a jar stay cookie-less.
            run.run_journey("catalog.hurl", tenant="nursery", odoo_base="http://x")
            assert "--cookie" not in calls[-1] and "--cookie-jar" not in calls[-1]
    finally:
        run.subprocess.run = real_run


if __name__ == "__main__":
    test_otlp_shape()
    test_empty_results()
    test_cart_cookie_jar_per_tenant_per_day()
    test_run_journey_passes_cookie_jar()
    print("ok: synthetic runner OTLP builder + cart cookie jars", file=sys.stderr)
