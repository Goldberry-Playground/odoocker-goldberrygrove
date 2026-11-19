#!/usr/bin/env python3
"""Regression test: every deployed ADDONS_PATH carries the image's third-party-addons dir.

No network, no docker. The grove-odoo image bakes OCA addons into
THIRD_PARTY_ADDONS (docker-odoo.yml build arg; odoo/third-party-addons.txt).
Odoo only looks where addons_path points, and on a droplet addons_path is the
ADDONS_PATH line cloud-init writes to /etc/grove/.env. Both lists that install
grove_schedule_f (AUTO_UPGRADE_MODULES, prod + QA) depend on addons in that
dir, so a template without it crash-loops odoo on the boot --init. That is
what happened at the 2026-11-16 QA train-up (GOL-3939). Pinned:

  templates   prod, QA and preview cloud-init render an ADDONS_PATH that
              contains the workflow's THIRD_PARTY_ADDONS, and keeps
              /workspace/current (the git-synced grove modules).
  smoke       the image CI's full-stack smoke boots on the same line, so an
              image that cannot start on it fails before it is published.

    python3 scripts/test_addons_path_third_party.py
"""

from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENVS = "infra/terraform/environments"
_WORKFLOW = ".github/workflows/docker-odoo.yml"
TEMPLATES = (
    f"{_ENVS}/production/cloud-init-odoo.yaml.tpl",
    f"{_ENVS}/qa-app-platform/cloud-init.yaml.tpl",
    f"{_ENVS}/preview/cloud-init.yaml.tpl",
)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _third_party_dir() -> str:
    m = re.search(r'^\s+THIRD_PARTY_ADDONS:\s*"([^"]+)"\s*$', _read(_WORKFLOW), re.M)
    assert m, f"{_WORKFLOW} no longer sets env THIRD_PARTY_ADDONS"
    return m.group(1)


def _addons_paths(rel: str) -> list[list[str]]:
    """Every uncommented ADDONS_PATH=... line in the file, split on commas."""
    return [m.split(",") for m in re.findall(r"^\s+ADDONS_PATH=(\S+)\s*$", _read(rel), re.M)]


def _assert_path(rel: str) -> None:
    third = _third_party_dir()
    paths = _addons_paths(rel)
    assert len(paths) == 1, f"{rel}: expected exactly one ADDONS_PATH line, found {len(paths)}"
    assert third in paths[0], f"{rel}: ADDONS_PATH lacks {third} (the OCA addons would be invisible to odoo)"
    assert "/workspace/current" in paths[0], f"{rel}: ADDONS_PATH lost /workspace/current (the grove modules)"


def test_templates():
    for rel in TEMPLATES:
        _assert_path(rel)


def test_smoke():
    _assert_path(_WORKFLOW)
    assert _addons_paths(_WORKFLOW)[0] == _addons_paths(TEMPLATES[0])[0], (
        "the image smoke no longer boots on the ADDONS_PATH prod renders"
    )


def test_templates_agree():
    first = _addons_paths(TEMPLATES[0])[0]
    for rel in TEMPLATES[1:]:
        assert _addons_paths(rel)[0] == first, f"{rel}: ADDONS_PATH differs from {TEMPLATES[0]}"


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
