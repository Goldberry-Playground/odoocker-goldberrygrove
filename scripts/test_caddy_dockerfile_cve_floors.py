#!/usr/bin/env python3
"""Regression tests for the grove-caddy Dockerfile's CVE floors.

No network, no Docker. The Trivy gate in docker-caddy.yml fails on any fixable
HIGH, and the image floats on caddy:2, so a dropped floor only shows up at the
next rebuild, as a red build on an unrelated PR (odoocker-goldberrygrove#960).

  go-module-floors   xcaddy must force the patched modules into the binary:
                     x/net >= 0.60.0 (CVE-2026-78669), go-jose/v4 >= 4.1.6
                     (CVE-2026-116587).
  apk-upgrades-all   The runtime stage upgrades every installed package, not a
                     named few, because brotli-libs and curl drifted in after
                     c-ares was patched by name.

    python3 scripts/test_caddy_dockerfile_cve_floors.py
"""

from __future__ import annotations

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DOCKERFILE = "infra/terraform/environments/qa-app-platform/compose/caddy.Dockerfile"

FLOORS = {
    "golang.org/x/net/http2": (0, 60, 0),
    "github.com/go-jose/go-jose/v4": (4, 1, 6),
}


def _instructions() -> str:
    """The Dockerfile with comments dropped, so prose can't satisfy a check."""
    with open(os.path.join(_ROOT, DOCKERFILE), encoding="utf-8") as fh:
        return "".join(ln for ln in fh if not ln.lstrip().startswith("#"))


class CaddyCveFloorTests(unittest.TestCase):
    def test_go_module_floors(self) -> None:
        text = _instructions()
        for module, floor in FLOORS.items():
            m = re.search(rf"--with {re.escape(module)}@v(\d+)\.(\d+)\.(\d+)\b", text)
            self.assertIsNotNone(m, f"{module} floor is missing from xcaddy build")
            found = tuple(int(g) for g in m.groups())
            self.assertGreaterEqual(found, floor, f"{module} floor is below the fixed version")

    def test_apk_upgrades_all_packages(self) -> None:
        m = re.search(r"^RUN apk upgrade (.*)$", _instructions(), re.MULTILINE)
        self.assertIsNotNone(m, "runtime stage must run apk upgrade")
        args = [a for a in m.group(1).split() if not a.startswith("-")]
        self.assertEqual(args, [], f"apk upgrade must not name packages: {args}")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
