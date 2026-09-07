#!/usr/bin/env python3
"""Regression tests for the Odoo Caddyfile templates (GOL-2150).

No network, no Caddy binary. These parse the checked-in `.tpl` files and assert
the routing properties that actually broke on prod:

  websocket-on-evented-port  Odoo 19 moved the bus from HTTP longpolling to a
                             real websocket at `/websocket`. Like /longpolling
                             it binds on the EVENTED worker (8072), not the HTTP
                             worker (8069). Routing it to 8069 makes every bus
                             connection 500 with "Couldn't bind the websocket"
                             -- chat, activity toasts and live inventory counts
                             all go dead while the rest of Odoo looks fine.
  longpolling-still-routed   The pre-19 endpoint keeps working; adding the
                             websocket path must not displace it.
  catch-all-on-http-port     Everything else still reaches 8069. If a broadened
                             matcher swallowed normal traffic onto the evented
                             worker the whole site would degrade.
  prod-and-qa-agree          BOTH environments carry the rule. Prod was fixed
                             first and QA was missed; QA runs the same odoo:19
                             image, so it had the identical dead bus. Any future
                             environment added here must satisfy the same rules.

    python3 scripts/test_caddy_odoo_routes.py
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Every Caddyfile template that fronts Odoo. Add new environments here — the
# tests below run against each one, so a new front door cannot silently ship
# without the bus routing.
ODOO_CADDYFILES = {
    "production": "infra/terraform/environments/production/compose/Caddyfile-odoo.tpl",
    "qa-app-platform": "infra/terraform/environments/qa-app-platform/compose/Caddyfile.tpl",
}

EVENTED_PORT = "8072"
HTTP_PORT = "8069"


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _longpoll_paths(src: str) -> list[str]:
    """The path tokens inside the `@longpoll { path ... }` named matcher."""
    block = re.search(r"@longpoll\s*\{(.*?)\}", src, re.S)
    assert block, "no @longpoll named matcher found"
    line = re.search(r"^\s*path\s+(.+?)\s*$", block.group(1), re.M)
    assert line, "@longpoll matcher has no `path` directive"
    return line.group(1).split()


def _matcher_upstream(src: str) -> str:
    m = re.search(r"^\s*reverse_proxy\s+@longpoll\s+odoo:(\d+)", src, re.M)
    assert m, "no `reverse_proxy @longpoll odoo:<port>` directive found"
    return m.group(1)


def _catchall_upstream(src: str) -> str:
    m = re.search(r"^\s*reverse_proxy\s+odoo:(\d+)", src, re.M)
    assert m, "no catch-all `reverse_proxy odoo:<port>` directive found"
    return m.group(1)


def test_websocket_routed_to_evented_worker():
    for env, rel in ODOO_CADDYFILES.items():
        src = _read(rel)
        paths = _longpoll_paths(src)
        assert any(p.startswith("/websocket") for p in paths), (
            f"{env} ({rel}): the @longpoll matcher does not include a /websocket "
            f"path, so the Odoo 19 bus would be proxied to the HTTP worker and "
            f"500 with \"Couldn't bind the websocket\". paths={paths}"
        )
        port = _matcher_upstream(src)
        assert port == EVENTED_PORT, (
            f"{env} ({rel}): @longpoll proxies to odoo:{port}, expected the "
            f"evented worker odoo:{EVENTED_PORT}"
        )


def test_longpolling_still_routed():
    for env, rel in ODOO_CADDYFILES.items():
        paths = _longpoll_paths(_read(rel))
        assert any(p.startswith("/longpolling") for p in paths), (
            f"{env} ({rel}): /longpolling was dropped from the @longpoll matcher; "
            f"pre-19 bus clients would fall through to the HTTP worker. paths={paths}"
        )


def test_catch_all_stays_on_http_worker():
    for env, rel in ODOO_CADDYFILES.items():
        port = _catchall_upstream(_read(rel))
        assert port == HTTP_PORT, (
            f"{env} ({rel}): the catch-all reverse_proxy points at odoo:{port}; "
            f"normal traffic must reach the HTTP worker odoo:{HTTP_PORT}"
        )


def test_every_odoo_caddyfile_is_covered():
    # Guards the map above against a new environment being added without its
    # routing asserted. Any .tpl that proxies to the evented port must be listed.
    listed = set(ODOO_CADDYFILES.values())
    found = set()
    for base, _dirs, names in os.walk(os.path.join(_ROOT, "infra", "terraform")):
        for name in names:
            if not name.endswith(".tpl"):
                continue
            rel = os.path.relpath(os.path.join(base, name), _ROOT)
            if f"odoo:{EVENTED_PORT}" in _read(rel):
                found.add(rel)
    assert found == listed, (
        "Caddyfile templates proxying to the evented worker do not match "
        f"ODOO_CADDYFILES.\n  unlisted: {sorted(found - listed)}\n"
        f"  listed but no longer routing: {sorted(listed - found)}"
    )


def _run():
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
