#!/usr/bin/env python3
"""Regression tests for the blogs droplet memory budget (2026-11-03).

No network, no Docker. These parse the checked-in compose file and cloud-init
template for grove-prod-blogs (s-2vcpu-2gb) and assert the caps that keep it
under the `[prod] Droplet memory > 85%` alert line:

  mysql-capped             performance_schema off (~150-200MB nobody reads),
                           buffer pool pinned so a future mysql image default
                           cannot silently grow it, connections bounded.
  every-ghost-heap-capped  Uncapped, V8 sizes the old-space heap off host RAM
                           and it creeps (~1%/day of the box, observed
                           10-28 -> 11-03). Every ghost-* service carries the
                           same cap, so a fifth tenant cannot ship without one.
  swap-before-stack        cloud-init creates a persistent swapfile BEFORE the
                           stack comes up, so first-boot spikes (image pulls,
                           MySQL init, 4x Ghost migrations) have headroom.

These files reach the box only via a deliberate `-replace` (blogs.tf ignores
user_data), so a regression here would sit unnoticed until the next replace.

    python3 scripts/test_blogs_memory_budget.py
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PROD = "infra/terraform/environments/production"
COMPOSE = f"{PROD}/compose/docker-compose.blogs.yml"
CLOUD_INIT = f"{PROD}/cloud-init-blogs.yaml.tpl"

# Per-tenant heap ceiling. 4x this plus ~100MB of non-heap per Node process,
# plus MySQL, must stay well inside 2GB; raising it is a sizing decision, not a
# drive-by edit.
MAX_GHOST_HEAP_MB = 256
MAX_MYSQL_CONNECTIONS = 100


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _services(src: str) -> dict[str, str]:
    """Top-level compose services -> their indented body text."""
    body = re.search(r"^services:\n(.*?)(?=^\S)", src, re.S | re.M)
    assert body, "no top-level `services:` block found"
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\s*\n", body.group(1), flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_mysql_memory_capped():
    mysql = _services(_read(COMPOSE)).get("mysql")
    assert mysql is not None, f"{COMPOSE}: no `mysql` service"
    assert re.search(r"--performance-schema=OFF\b", mysql), (
        f"{COMPOSE}: mysql no longer passes --performance-schema=OFF"
    )
    pool = re.search(r"--innodb-buffer-pool-size=(\d+)M\b", mysql)
    assert pool and int(pool.group(1)) <= 128, (
        f"{COMPOSE}: mysql buffer pool must be pinned at <=128M, got "
        f"{pool.group(0) if pool else 'unset (image default)'}"
    )
    conns = re.search(r"--max-connections=(\d+)\b", mysql)
    assert conns and int(conns.group(1)) <= MAX_MYSQL_CONNECTIONS, (
        f"{COMPOSE}: mysql max-connections must be set and <= "
        f"{MAX_MYSQL_CONNECTIONS}, got {conns.group(0) if conns else 'unset'}"
    )


def test_every_ghost_heap_capped():
    ghosts = {
        name: body
        for name, body in _services(_read(COMPOSE)).items()
        if re.search(r"^\s*image:\s*ghost:", body, re.M)
    }
    assert len(ghosts) >= 4, f"{COMPOSE}: expected >=4 ghost services, found {sorted(ghosts)}"
    for name, body in ghosts.items():
        m = re.search(r"NODE_OPTIONS:\s*[\"']?[^\n]*--max-old-space-size=(\d+)", body)
        assert m, f"{COMPOSE}: {name} has no NODE_OPTIONS --max-old-space-size cap"
        assert int(m.group(1)) <= MAX_GHOST_HEAP_MB, (
            f"{COMPOSE}: {name} heap cap {m.group(1)}MB exceeds {MAX_GHOST_HEAP_MB}MB"
        )


def test_swap_created_before_stack_up():
    src = _read(CLOUD_INIT)
    for needle in ("fallocate -l 1G /swapfile", "mkswap /swapfile", "swapon /swapfile"):
        assert needle in src, f"{CLOUD_INIT}: missing `{needle}`"
    assert re.search(r"/swapfile none swap sw 0 0", src), (
        f"{CLOUD_INIT}: swapfile not persisted to /etc/fstab (lost on reboot)"
    )
    assert "vm.swappiness=10" in src, f"{CLOUD_INIT}: swappiness not lowered"
    swap_at = src.index("swapon /swapfile")
    up_at = src.index("docker compose -f /etc/grove-blogs/docker-compose.yml")
    assert swap_at < up_at, f"{CLOUD_INIT}: swap is enabled AFTER the stack comes up"


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
