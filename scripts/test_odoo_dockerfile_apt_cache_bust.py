#!/usr/bin/env python3
"""Regression tests for the odoo image's apt layer-cache bust (GOL-3780).

No network, no Docker, no PyYAML. The apt RUN in odoo/Dockerfile exists to
pull security patches published after the upstream odoo:19 image was built,
but a builder using `cache-from: type=gha` restores that layer on its
instruction text alone, so it never re-runs and the image silently stops
receiving patches. Security Scans run 42121264782 flagged apt 2.8.3
(CVE-2026-109518) a day after the fix reached noble-security because the
layer was CACHED from the previous green run.

  dockerfile-arg     odoo/Dockerfile declares ARG APT_CACHE_BUST before the
                     apt-get upgrade RUN and references it inside that RUN,
                     so the value is part of the layer's cache key.
  workflows-plumb    Every CI build of odoo/Dockerfile that uses a gha layer
                     cache passes a non-constant APT_CACHE_BUST build-arg
                     (docker-odoo.yml: ISO-week, #986), and the Security
                     Scans build passes a per-run one so a red scan means the
                     archive has no fix yet, never "the layer was cached".

    python3 scripts/test_odoo_dockerfile_apt_cache_bust.py
"""

from __future__ import annotations

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DOCKERFILE = "odoo/Dockerfile"
ARG_NAME = "APT_CACHE_BUST"

# Workflows that build odoo/Dockerfile behind a GitHub Actions layer cache,
# mapped to the substring their APT_CACHE_BUST value must carry. The scan
# must be per-run; the publish pipeline's weekly key (#986) is a cost choice,
# so it only has to be an expression rather than a constant.
CACHED_BUILD_WORKFLOWS = {
    ".github/workflows/security-scans.yml": "${{ github.run_id }}",
    ".github/workflows/docker-odoo.yml": "${{",
}


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _instructions(rel: str) -> list[str]:
    """Dockerfile instructions with comments dropped and continuations joined."""
    lines = [ln for ln in _read(rel).splitlines() if not ln.lstrip().startswith("#")]
    joined: list[str] = []
    buf = ""
    for ln in lines:
        if ln.rstrip().endswith("\\"):
            buf += ln.rstrip()[:-1] + " "
            continue
        joined.append(buf + ln)
        buf = ""
    if buf:
        joined.append(buf)
    return [ln for ln in joined if ln.strip()]


def _build_arg_blocks(workflow_text: str) -> list[list[str]]:
    """Each `build-args: |` block as its list of KEY=VALUE lines."""
    blocks: list[list[str]] = []
    lines = workflow_text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)build-args:\s*\|\s*$", ln)
        if not m:
            continue
        indent = len(m.group(1))
        block: list[str] = []
        for nxt in lines[i + 1 :]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            block.append(nxt.strip())
        blocks.append(block)
    return blocks


class OdooAptCacheBustTests(unittest.TestCase):
    def _apt_run(self) -> tuple[int, str]:
        instrs = _instructions(DOCKERFILE)
        for idx, ln in enumerate(instrs):
            if ln.startswith("RUN") and "apt-get upgrade" in ln:
                return idx, ln
        self.fail("odoo/Dockerfile must have a RUN that runs apt-get upgrade")

    def test_arg_declared_before_apt_run(self) -> None:
        instrs = _instructions(DOCKERFILE)
        run_idx, _ = self._apt_run()
        decl = [i for i, ln in enumerate(instrs) if re.match(rf"^ARG {ARG_NAME}\b", ln)]
        self.assertTrue(decl, f"ARG {ARG_NAME} is not declared in {DOCKERFILE}")
        self.assertLess(decl[0], run_idx, f"ARG {ARG_NAME} must precede the apt-get upgrade RUN")

    def test_apt_run_references_arg(self) -> None:
        _, run = self._apt_run()
        self.assertRegex(
            run,
            rf"\$\{{{ARG_NAME}\}}|\${ARG_NAME}\b",
            f"the apt-get upgrade RUN must reference ${ARG_NAME} so it is in the layer cache key",
        )

    def test_cached_workflows_pass_changing_value(self) -> None:
        for rel, needle in CACHED_BUILD_WORKFLOWS.items():
            text = _read(rel)
            with self.subTest(workflow=rel):
                self.assertIn(
                    "cache-from: type=gha",
                    text,
                    f"{rel} no longer uses a gha cache; drop it from CACHED_BUILD_WORKFLOWS",
                )
                odoo_blocks = [
                    b for b in _build_arg_blocks(text) if any(ln.startswith("ODOO_TAG=") for ln in b)
                ]
                self.assertTrue(odoo_blocks, f"{rel}: no build-args block with ODOO_TAG= found")
                for block in odoo_blocks:
                    vals = [ln.split("=", 1)[1] for ln in block if ln.startswith(f"{ARG_NAME}=")]
                    self.assertEqual(
                        len(vals), 1, f"{rel}: odoo build-args must set {ARG_NAME} exactly once: {block}"
                    )
                    self.assertIn(
                        needle,
                        vals[0],
                        f"{rel}: {ARG_NAME} must contain {needle!r}, got {vals[0]!r}",
                    )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
