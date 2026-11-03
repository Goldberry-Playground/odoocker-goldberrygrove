#!/usr/bin/env python3
"""Regression tests for the app-plane collector's Docker access.

No network, no Docker, no collector binary. These read the checked-in config
and compose files and assert the two properties that each crash-looped the
prod otel-collector on 2026-11-03 (one failing receiver stops the whole
collector, so hostmetrics + postgres went dark along with docker_stats):

  api-version-pinned   docker_stats defaults to Docker API 1.25; prod's daemon
                       rejects anything below 1.40 ("client version 1.25 is
                       too old"). The pin must exist and be >= 1.40.
  gid-not-hardcoded    The collector reaches docker.sock via group_add. A
                       literal gid (988 = the QA box) is wrong on prod (987);
                       it must come from ${DOCKER_GID}, which the deploy reads
                       off the host's socket.

    python3 scripts/test_otelcol_docker_stats.py
"""

from __future__ import annotations

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

OTELCOL_CONFIG = "otel/otelcol-config.yaml"
MIN_API = (1, 40)
# Every compose file that runs a collector with the Docker socket mounted.
COLLECTOR_COMPOSE_FILES = [
    "docker-compose.monitoring.app-plane.yml",
    "docker-compose.apm.yml",
]


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _docker_stats_block(text: str) -> str:
    """Lines of the `docker_stats:` receiver, up to the next sibling key."""
    m = re.search(r"^(\s*)docker_stats:\s*$", text, re.M)
    if not m:
        raise AssertionError(f"{OTELCOL_CONFIG}: no docker_stats receiver")
    indent = len(m.group(1))
    out = []
    for line in text[m.end():].splitlines()[1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            if len(line) - len(line.lstrip()) <= indent:
                break
        out.append(line)
    return "\n".join(out)


class DockerStatsApiVersion(unittest.TestCase):
    def test_api_version_pinned_at_least_1_40(self):
        block = _docker_stats_block(_read(OTELCOL_CONFIG))
        m = re.search(r'^\s*api_version:\s*"?(\d+)\.(\d+)"?\s*$', block, re.M)
        self.assertIsNotNone(m, "docker_stats.api_version is unset (defaults to 1.25)")
        self.assertGreaterEqual((int(m.group(1)), int(m.group(2))), MIN_API)


class CollectorDockerGid(unittest.TestCase):
    def test_group_add_uses_docker_gid_var(self):
        for rel in COLLECTOR_COMPOSE_FILES:
            with self.subTest(compose=rel):
                text = _read(rel)
                m = re.search(r"^\s*group_add:\s*\n((?:\s*(?:-|#).*\n)+)", text, re.M)
                self.assertIsNotNone(m, f"{rel}: no group_add for docker.sock")
                self.assertIn("${DOCKER_GID", m.group(1),
                              f"{rel}: group_add gid is hard-coded")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
