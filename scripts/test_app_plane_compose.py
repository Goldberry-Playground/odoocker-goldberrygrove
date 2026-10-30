#!/usr/bin/env python3
"""Regression tests for the app-plane monitoring go-live defects (GOL-2329).

The first prod bring-up of docker-compose.monitoring.app-plane.yml on
grove-prod-odoo (2026-10-29) came up but not healthy:

  otelcol-pin           OTELCOL_TAG=0.115.0 was never published (404 on Docker
                        Hub + ghcr); every pin must name a real tag and agree.
  docker-gid-not-literal  group_add "988" was QA's docker gid; prod's differs,
                        so the non-root collector got EACCES on docker.sock and
                        crash-looped. The gid must come from the host
                        (DOCKER_GID), and compose must refuse to start without it.
  render-adds-docker-gid  the documented env-file render appends DOCKER_GID.
  monitors-json-mounted   probes.emit died with FileNotFoundError because the
                        runner's build context is ./synthetic only.
  ghost-empty-blog-ok   ghost-content asserted posts count > 0, so nursery's
                        empty (but healthy, key-valid) Ghost paged as down.
  app-plane-no-keep-warning  setup-monitoring.py on MONITORING_PLANE=app warned
                        "alerts will fail to notify" though the obs plane owns
                        the destination.

Text-only (no PyYAML on CI runners), no network.

    python3 scripts/test_app_plane_compose.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PLANE = ROOT / "docker-compose.monitoring.app-plane.yml"
APM = ROOT / "docker-compose.apm.yml"
ENV_OP = ROOT / "scripts" / "prod-monitoring.env.op"
RUNBOOK = ROOT / "docs" / "RUNBOOK-prod-synthetics-golive.md"

# Tags known NOT to exist upstream — pinning one breaks the pull.
UNPUBLISHED_OTELCOL = {"0.115.0"}


def otelcol_pins() -> dict[str, str]:
    pins = {}
    for f in ("docker-compose.monitoring.yml", "docker-compose.apm.yml",
              "docker-compose.monitoring.app-plane.yml"):
        m = re.search(r"opentelemetry-collector-contrib:\$\{OTELCOL_TAG:-([^}]+)\}", (ROOT / f).read_text())
        assert m, f"{f}: otelcol image line not found"
        pins[f] = m.group(1)
    for f in ("scripts/prod-monitoring.env.op", ".env.monitoring.example"):
        m = re.search(r'^OTELCOL_TAG="?([^"\n]+)"?$', (ROOT / f).read_text(), re.MULTILINE)
        assert m, f"{f}: OTELCOL_TAG not found"
        pins[f] = m.group(1)
    return pins


def group_add_entries(text: str) -> list[str]:
    m = re.search(r"^ {4}group_add:\n((?: {6}(?:- |#).*\n)+)", text, re.MULTILINE)
    assert m, "group_add block not found"
    return [ln.strip()[2:].strip().strip('"') for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]


class AppPlaneComposeTests(unittest.TestCase):
    def test_otelcol_pins_published_and_consistent(self) -> None:
        pins = otelcol_pins()
        for f, tag in pins.items():
            self.assertNotIn(tag, UNPUBLISHED_OTELCOL, f"{f} pins unpublished otelcol {tag}")
            self.assertNotEqual(tag, "latest", f"{f} floats otelcol on :latest")
        self.assertEqual(len(set(pins.values())), 1, f"otelcol pins disagree: {pins}")

    def test_docker_gid_comes_from_host_not_literal(self) -> None:
        for f in (APP_PLANE, APM):
            for entry in group_add_entries(f.read_text()):
                self.assertFalse(entry.isdigit(), f"{f.name}: hardcoded gid {entry}")
                self.assertTrue(entry.startswith("${DOCKER_GID:?"),
                                f"{f.name}: group_add must require DOCKER_GID, got {entry}")

    def test_render_appends_docker_gid(self) -> None:
        want = 'DOCKER_GID=$(getent group docker | cut -d: -f3)'
        for f in (ENV_OP, RUNBOOK):
            self.assertIn(want, f.read_text(), f"{f.name}: render step must append DOCKER_GID")

    def test_monitors_json_mounted_into_runner(self) -> None:
        text = APP_PLANE.read_text()
        runner = text.split("  synthetic-runner:", 1)[1]
        self.assertIn("./openobserve/monitors.json:/app/monitors.json:ro", runner)
        self.assertTrue((ROOT / "openobserve" / "monitors.json").is_file())

    def test_ghost_content_tolerates_empty_blog(self) -> None:
        hurl = (ROOT / "synthetic" / "journeys" / "ghost-content.hurl").read_text()
        asserts = [ln for ln in hurl.splitlines() if ln.startswith("jsonpath")]
        self.assertIn('jsonpath "$.posts" isCollection', asserts)
        self.assertFalse(any("count > 0" in a or "posts[0]" in a for a in asserts), asserts)


class AppPlaneKeepWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("setup_monitoring", ROOT / "scripts" / "setup-monitoring.py")
        self.mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(self.mod)
        self.logged: list[str] = []
        self.mod._log = lambda *a, **_k: self.logged.append(" ".join(map(str, a)))
        self._env = os.environ.get("MONITORING_PLANE")

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("MONITORING_PLANE", None)
        else:
            os.environ["MONITORING_PLANE"] = self._env

    def test_app_plane_does_not_warn_about_keep_destination(self) -> None:
        os.environ["MONITORING_PLANE"] = "app"
        self.mod.oo_upsert_destination({"name": "keep-webhook", "url": ""})
        self.assertTrue(self.logged)
        self.assertFalse(any("⚠" in ln or "fail" in ln for ln in self.logged), self.logged)

    def test_obs_plane_still_warns(self) -> None:
        os.environ["MONITORING_PLANE"] = "obs"
        self.mod.oo_upsert_destination({"name": "keep-webhook", "url": ""})
        self.assertTrue(any("⚠" in ln and "KEEP_EVENT_URL unset" in ln for ln in self.logged), self.logged)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
