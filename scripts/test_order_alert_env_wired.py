#!/usr/bin/env python3
"""Regression test: the odoo service gets the Discord order-card env (grove-odoo-modules#408).

No network, no docker. grove_headless reads ORDER_ALERT_SECRET and
DISCORD_BRIDGE_BASE_URL from os.environ to POST each new order to the Discord
bridge's /orders/alert (the interactive card with hold / move / Mark shipped).
The odoo process env comes ONLY from the compose `environment:` block, the same
footgun that once silenced the Discord alerts and the publish webhook. Pinned:

  passthrough-prod   docker-compose.odoo.yml passes both, defaulting to empty.
  passthrough-qa     docker-compose.qa.yml passes both, defaulting to empty, so
                     QA exercises the same path before a promote.

Empty is the safe default: _post_order_alert_card() returns False and the plain
webhook ping still fires, so landing this changes nothing until .env sets both.

    python3 scripts/test_order_alert_env_wired.py
"""

from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENVS = os.path.join(_ROOT, "infra/terraform/environments")
KEYS = ("ORDER_ALERT_SECRET", "DISCORD_BRIDGE_BASE_URL")


def _odoo_env_block(rel: str) -> str:
    """The `environment:` mapping of the `odoo:` service, as text."""
    with open(os.path.join(_ENVS, rel), encoding="utf-8") as fh:
        text = fh.read()
    service = re.search(r"^  odoo:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    assert service, f"no odoo service in {rel}"
    env = re.search(r"^    environment:\n((?:^      .*\n|^\s*\n)*)", service.group(1), re.M)
    assert env, f"odoo service in {rel} has no environment block"
    return env.group(1)


def _assert_passthrough(rel: str) -> None:
    block = _odoo_env_block(rel)
    for key in KEYS:
        assert re.search(rf"^\s+{key}: \$\{{{key}:-\}}\s*$", block, re.M), (
            f"{key} is not passed through the odoo service environment in {rel}"
        )


def test_passthrough_prod():
    _assert_passthrough("production/compose/docker-compose.odoo.yml")


def test_passthrough_qa():
    _assert_passthrough("qa-app-platform/compose/docker-compose.qa.yml")


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
