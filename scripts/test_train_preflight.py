#!/usr/bin/env python3
"""Regression tests for scripts/train-preflight.sh (GOL-2584).

`make train-up` runs this first, so a false FAIL here blocks the whole release
train. Both regressions below did exactly that on 2026-10-05:

  * on macOS the configured doctl token was never found (wrong config path, and
    a `\\S` sed class that BSD sed silently ignores) -> "no DigitalOcean token";
  * once #790 landed, section [2] ran tf-state-lock-check.sh with no mode and no
    credentials -> exit 2 -> FAIL on every run, ACK escape no longer reachable.

The real script runs against a local stub DO API (TRAIN_PREFLIGHT_DO_API) and a
throwaway HOME -- no network, no credentials:

  env-token-wins                 DIGITALOCEAN_TOKEN beats any doctl config.
  macos-doctl-config             token read from ~/Library/Application Support.
  linux-xdg-doctl-config         token read from $XDG_CONFIG_HOME/doctl.
  default-context-only           only the column-0 access-token is used; quoted
                                 values are unquoted; other contexts ignored.
  no-token-exits-2               nothing configured -> exit 2, clear message.
  volumes-ok-and-lock-wired      healthy volumes + guard wired -> PASS (exit 0).
  missing-volume-fails           a durable volume gone -> FAIL (exit 1).
  lock-guard-unwired-fails       guard call removed -> FAIL, ACK -> warn + pass.

    python3 scripts/test_train_preflight.py
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

HEALTHY = [
    {"name": "nyc3-grove-prod-odoo-filestore", "droplet_ids": [1]},
    {"name": "nyc3-grove-prod-blogs-data", "droplet_ids": [2]},
    {"name": "nyc3-grove-qa-l3-odoo-filestore", "droplet_ids": []},
    {"name": "nyc3-grove-qa-l3-caddy-data", "droplet_ids": []},
]


class _StubDO:
    def __init__(self, volumes):
        self.auth: list[str] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                stub.auth.append(self.headers.get("Authorization", ""))
                body = json.dumps({"volumes": volumes}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run(stub, home, extra=None, cwd=_ROOT):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DIGITALOCEAN_", "TRAIN_PREFLIGHT_", "XDG_"))}
    env.update(HOME=home, TRAIN_PREFLIGHT_DO_API=stub.url)
    env.update(extra or {})
    return subprocess.run(["bash", "scripts/train-preflight.sh"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _case(volumes=HEALTHY):
    return _StubDO(volumes), tempfile.mkdtemp(prefix="pf-home-")


def test_env_token_wins():
    stub, home = _case()
    try:
        _write(f"{home}/.config/doctl/config.yaml", "access-token: dop_v1_fromconfig\n")
        _run(stub, home, {"DIGITALOCEAN_TOKEN": "dop_v1_fromenv"})
        assert stub.auth == ["Bearer dop_v1_fromenv"], stub.auth
    finally:
        stub.close(); shutil.rmtree(home)


def test_macos_doctl_config():
    stub, home = _case()
    try:
        _write(f"{home}/Library/Application Support/doctl/config.yaml",
               "context: default\naccess-token: dop_v1_mac\n")
        r = _run(stub, home)
        assert stub.auth == ["Bearer dop_v1_mac"], (stub.auth, r.stdout, r.stderr)
        assert "no DigitalOcean token" not in r.stderr, r.stderr
    finally:
        stub.close(); shutil.rmtree(home)


def test_linux_xdg_doctl_config():
    stub, home = _case()
    try:
        xdg = os.path.join(home, "xdg")
        _write(f"{xdg}/doctl/config.yaml", "access-token: dop_v1_linux\n")
        _run(stub, home, {"XDG_CONFIG_HOME": xdg})
        assert stub.auth == ["Bearer dop_v1_linux"], stub.auth
    finally:
        stub.close(); shutil.rmtree(home)


def test_default_context_only():
    stub, home = _case()
    try:
        _write(f"{home}/.config/doctl/config.yaml",
               "auth-contexts:\n  access-token: dop_v1_stale_indented\n"
               'access-token: "dop_v1_default"\ncontext: default\n')
        _run(stub, home)
        assert stub.auth == ["Bearer dop_v1_default"], stub.auth
    finally:
        stub.close(); shutil.rmtree(home)


def test_no_token_exits_2():
    stub, home = _case()
    try:
        r = _run(stub, home)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert "no DigitalOcean token" in r.stderr, r.stderr
        assert stub.auth == [], "must not call the API without a token"
    finally:
        stub.close(); shutil.rmtree(home)


def test_volumes_ok_and_lock_wired():
    stub, home = _case()
    try:
        r = _run(stub, home, {"DIGITALOCEAN_TOKEN": "t"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "PREFLIGHT PASS" in r.stdout, r.stdout
        assert "held-lock guard wired" in r.stdout, r.stdout
    finally:
        stub.close(); shutil.rmtree(home)


def test_missing_volume_fails():
    stub, home = _case([v for v in HEALTHY if "caddy" not in v["name"]])
    try:
        r = _run(stub, home, {"DIGITALOCEAN_TOKEN": "t"})
        assert r.returncode == 1, (r.returncode, r.stdout)
        assert "nyc3-grove-qa-l3-caddy-data MISSING" in r.stdout, r.stdout
    finally:
        stub.close(); shutil.rmtree(home)


def test_lock_guard_unwired_fails():
    # Copy the repo files the preflight reads into a scratch tree, then strip
    # the guard call from the Makefile there.
    stub, home = _case()
    tree = tempfile.mkdtemp(prefix="pf-tree-")
    try:
        os.makedirs(f"{tree}/scripts")
        for rel in ("scripts/train-preflight.sh", "scripts/tf-state-lock-check.sh",
                    "scripts/qa-l3-teardown.sh", "Makefile"):
            shutil.copy(os.path.join(_ROOT, rel), os.path.join(tree, rel))
        with open(f"{tree}/Makefile") as f:
            mk = f.read().replace("tf-state-lock-check.sh guard", "tf-state-lock-check.sh removed")
        with open(f"{tree}/Makefile", "w") as f:
            f.write(mk)
        r = _run(stub, home, {"DIGITALOCEAN_TOKEN": "t"}, cwd=tree)
        assert r.returncode == 1, (r.returncode, r.stdout)
        assert "held-lock guard NOT wired" in r.stdout, r.stdout
        r = _run(stub, home, {"DIGITALOCEAN_TOKEN": "t", "TRAIN_PREFLIGHT_ACK_LOCK": "1"}, cwd=tree)
        assert r.returncode == 0, (r.returncode, r.stdout)
        assert "TRAIN_PREFLIGHT_ACK_LOCK=1" in r.stdout, r.stdout
    finally:
        stub.close(); shutil.rmtree(home); shutil.rmtree(tree)


def _run_all() -> int:
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
    sys.exit(_run_all())
