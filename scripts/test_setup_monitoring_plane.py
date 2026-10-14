#!/usr/bin/env python3
"""Regression tests for setup-monitoring.py's MONITORING_PLANE switch (GOL-2323).

`make monitoring-up` depends on `make monitoring-setup`, which runs
setup-monitoring.py. On the prod Odoo droplet (the APP plane) that script used
to wait 120s for a Keep backend that does not exist there -- grove-obs-fw admits
:8080 from admin IPs only (ADR-010) -- then `sys.exit(1)`, so the prod synthetic
runner could never be brought up by the documented command. The obs-plane
deploy on grove-obs owns the Keep bootstrap (GOL-2329).

No network. The real module is imported from its hyphenated filename and the
side-effecting steps are replaced with recorders, so what is asserted is the
step sequence main() actually drives:

  obs-plane-is-default       no MONITORING_PLANE -> waits for OO AND Keep,
                             bootstraps both (unchanged behaviour).
  app-plane-skips-keep       MONITORING_PLANE=app -> waits for OO only, OO
                             objects still upserted, NO Keep call of any kind.
  app-plane-needs-no-keep    with KEEP_WEBHOOK_TOKEN unset the app plane still
                             completes (require_env for Keep is never reached).
  bad-plane-is-config-error  MONITORING_PLANE=typo exits 1 before any wait.
  render-keeps-plane         the go-live `env | grep -E` render filter passes
                             MONITORING_ through, else the plane is dropped.
  prod-env-declares-app      scripts/prod-monitoring.env.op pins
                             MONITORING_PLANE="app" and OPENOBSERVE_BASE_URL on
                             grove-obs's private address (10.132.23.199:5080),
                             the two values the prod bring-up depends on.

    python3 scripts/test_setup_monitoring_plane.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "setup-monitoring.py"
ENV_OP = HERE / "prod-monitoring.env.op"


def load_module():
    spec = importlib.util.spec_from_file_location("setup_monitoring", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class PlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_module()
        self.calls: list[str] = []
        m = self.mod
        m._log = lambda *_a, **_k: None  # keep test output clean

        def rec(name):
            def _f(*_a, **_k):
                self.calls.append(name)
                return {"url": "http://example.invalid"} if name == "oo_bootstrap" else None
            return _f

        m.wait_for = lambda url, _s, *, name: self.calls.append(f"wait:{name}")
        for fn in ("maybe_seed_canary", "oo_bootstrap", "oo_bootstrap_alerts",
                   "oo_bootstrap_dashboards", "keep_bootstrap_providers",
                   "keep_bootstrap_workflows"):
            setattr(m, fn, rec(fn))
        self._env = dict(os.environ)
        for k in ("MONITORING_PLANE", "KEEP_WEBHOOK_TOKEN", "KEEP_BASE_URL"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_obs_plane_is_default(self) -> None:
        self.mod.main()
        self.assertEqual(self.calls, [
            "wait:OpenObserve", "wait:Keep",
            "maybe_seed_canary", "oo_bootstrap", "oo_bootstrap_alerts", "oo_bootstrap_dashboards",
            "keep_bootstrap_providers", "keep_bootstrap_workflows",
        ])

    def test_app_plane_skips_keep(self) -> None:
        os.environ["MONITORING_PLANE"] = "app"
        self.mod.main()
        self.assertEqual(self.calls, [
            "wait:OpenObserve",
            "maybe_seed_canary", "oo_bootstrap", "oo_bootstrap_alerts", "oo_bootstrap_dashboards",
        ])
        self.assertFalse(any("Keep" in c or "keep" in c for c in self.calls))

    def test_app_plane_needs_no_keep_secrets(self) -> None:
        os.environ["MONITORING_PLANE"] = "app"
        self.assertNotIn("KEEP_WEBHOOK_TOKEN", os.environ)
        # must not raise / SystemExit even though keep_headers() would require it
        self.mod.main()
        self.assertIn("oo_bootstrap", self.calls)

    def test_bad_plane_is_config_error(self) -> None:
        os.environ["MONITORING_PLANE"] = "prod"
        with self.assertRaises(SystemExit) as cm:
            self.mod.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertNotIn("wait:Keep", self.calls)

    def test_prod_env_declares_app_plane(self) -> None:
        text = ENV_OP.read_text()
        live = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
        self.assertIn('MONITORING_PLANE="app"', live)
        base = [ln for ln in live if ln.startswith("OPENOBSERVE_BASE_URL=")]
        self.assertEqual(len(base), 1, "exactly one OPENOBSERVE_BASE_URL")
        self.assertRegex(base[0], re.escape('OPENOBSERVE_BASE_URL="http://10.132.23.199:5080"'))
        # the app plane must not ALSO try to render Keep secrets
        self.assertFalse(any(ln.startswith("KEEP_WEBHOOK_TOKEN=") for ln in live))

    def test_render_filter_keeps_monitoring_plane(self) -> None:
        """The go-live render pipes `env` through a prefix grep; MONITORING_
        must be in it or the droplet's .env.monitoring silently drops the
        plane and the default (obs) waits on Keep again."""
        runbook = (HERE.parent / "docs" / "RUNBOOK-prod-synthetics-golive.md").read_text()
        for src, text in (("runbook", runbook), ("env.op", ENV_OP.read_text())):
            filters = re.findall(r'grep -E "\^\(([A-Z|]+)\)_"', text)
            self.assertTrue(filters, f"{src}: render grep not found")
            for f in filters:
                self.assertIn("MONITORING", f.split("|"), f"{src}: {f}")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
