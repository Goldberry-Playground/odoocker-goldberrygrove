#!/usr/bin/env python3
"""Regression tests for the prod guides publish-webhook wiring (hotfix, 2026-09-30).

No network, no terraform binary. These parse the checked-in production files and
assert the two halves of the Odoo -> storefront publish webhook (GOL-985/986)
stay wired to each other. On 2026-09-30 "Publish Guide to Storefront" failed on
prod with "Publish webhook is not configured for tenant 'nursery'" because
production had NEITHER half:

  sender-in-dotenv        cloud-init writes GROVE_PUBLISH_WEBHOOK_URL_NURSERY and
                          _SECRET_NURSERY into /etc/grove/.env.
  sender-reaches-odoo     the odoo service's compose environment passes both
                          through. grove_publish_event.py reads os.environ, and
                          the process env comes ONLY from that block (the same
                          footgun that silenced the Discord alerts).
  receiver-has-secret     the tenant apps set GROVE_PUBLISH_WEBHOOK_SECRET, which
                          tenant.secrets.ts reads; unset => /api/webhooks/publish
                          fails closed (401 on every delivery).
  same-secret-both-sides  sender and receiver resolve to the SAME TF var, so the
                          HMAC can never be signed with one value and checked
                          against another.
  template-var-passed     odoo.tf actually passes that var into the cloud-init
                          templatefile (an unpassed var fails at plan time, but
                          only on the next -target'ed rebuild).

    python3 scripts/test_publish_webhook_prod_wired.py
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PROD = os.path.join(_ROOT, "infra/terraform/environments/production")

SECRET_VAR = "grove_revalidate_secret"
SENDER_KEYS = ("GROVE_PUBLISH_WEBHOOK_URL_NURSERY", "GROVE_PUBLISH_WEBHOOK_SECRET_NURSERY")


def _read(rel: str) -> str:
    with open(os.path.join(_PROD, rel), encoding="utf-8") as fh:
        return fh.read()


def _dotenv_lines() -> dict[str, str]:
    out = {}
    for line in _read("cloud-init-odoo.yaml.tpl").splitlines():
        m = re.match(r"^\s*(GROVE_PUBLISH_WEBHOOK_[A-Z_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def test_sender_in_dotenv():
    env = _dotenv_lines()
    for key in SENDER_KEYS:
        assert key in env, f"{key} missing from cloud-init-odoo.yaml.tpl .env"
    url = env.get("GROVE_PUBLISH_WEBHOOK_URL_NURSERY", "")
    assert url.startswith("https://") and url.endswith("/api/webhooks/publish"), f"unexpected sender URL: {url}"


def test_sender_reaches_odoo():
    compose = _read("compose/docker-compose.odoo.yml")
    for key in SENDER_KEYS:
        assert re.search(rf"^\s+{key}: \$\{{{key}:-\}}\s*$", compose, re.M), (
            f"{key} is not passed through the odoo service environment in docker-compose.odoo.yml"
        )


def test_receiver_has_secret():
    apps = _read("apps.tf")
    tenant = apps[apps.index('resource "digitalocean_app" "tenant"'):]
    m = re.search(r'key\s*=\s*"GROVE_PUBLISH_WEBHOOK_SECRET"\s*\n\s*value\s*=\s*var\.(\w+)', tenant)
    assert m, "tenant apps do not set GROVE_PUBLISH_WEBHOOK_SECRET"
    assert m.group(1) == SECRET_VAR, f"receiver secret is var.{m.group(1)}, expected var.{SECRET_VAR}"


def test_same_secret_both_sides():
    sender = _dotenv_lines().get("GROVE_PUBLISH_WEBHOOK_SECRET_NURSERY")
    assert sender is not None, "GROVE_PUBLISH_WEBHOOK_SECRET_NURSERY missing from cloud-init .env"
    assert sender == "${" + SECRET_VAR + "}", (
        f"sender secret is {sender!r}; it must be ${{{SECRET_VAR}}} to match the receiver"
    )


def test_template_var_passed():
    odoo = _read("odoo.tf")
    block = odoo[odoo.index('templatefile("${path.module}/cloud-init-odoo.yaml.tpl"'):]
    assert re.search(rf"^\s+{SECRET_VAR}\s*=\s*var\.{SECRET_VAR}\s*$", block, re.M), (
        f"odoo.tf does not pass {SECRET_VAR} into the cloud-init templatefile"
    )


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
