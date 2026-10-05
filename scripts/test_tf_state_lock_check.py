#!/usr/bin/env python3
"""Regression tests for scripts/tf-state-lock-check.sh (GOL-2584).

The guard runs inside BOTH release-train legs (`make qa-l3-up` under `&&`,
`qa-l3-teardown.sh` under `set -e`), so its exit code decides whether a train
starts. These tests run the real script against a local stub S3 server -- no
Spaces, no credentials -- and pin the properties that matter:

  guard-no-lock-proceeds       404 on <key>.tflock -> exit 0.
  guard-held-lock-refuses      200 on <key>.tflock -> exit 1 and the lock body
                               (who holds it) is printed for the operator.
  guard-override               held lock + TF_LOCK_GUARD_OFF=1 -> exit 0.
  guard-inconclusive-proceeds  5xx / 403 -> exit 0 with a WARNING (advisory
                               guard must never become its own outage).
  guard-unreachable-proceeds   no HTTP answer at all (connection refused) ->
                               exit 0. Regression: this used to raise an
                               uncaught URLError and exit 1, aborting train-up.
  guard-reads-the-right-key    GET /<bucket>/<state-key>.tflock, SigV4-signed.
  guard-needs-creds            missing AWS_* -> exit 2 before any request.
  probe-enforcing-passes       backend returns 412 on the 2nd conditional PUT
                               -> exit 0 (locking works; guard can retire).
  probe-ignoring-fails         backend overwrites (DO Spaces today) -> exit 1,
                               and both PUTs carried If-None-Match: *.

    python3 scripts/test_tf_state_lock_check.py
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "tf-state-lock-check.sh")
STATE_KEY = "qa-app-platform/terraform.tfstate"
LOCK_PATH = f"/grove-tf-state/{STATE_KEY}.tflock"


class _Stub:
    """Tiny S3 stand-in. `mode` picks the behaviour; requests are recorded."""

    def __init__(self, mode: str, lock_status: int = 404, lock_body: str = ""):
        self.mode = mode
        self.lock_status = lock_status
        self.lock_body = lock_body
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # keep test output clean
                pass

            def _record(self):
                stub.requests.append(
                    (self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
                )

            def _reply(self, code: int, body: bytes = b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                if self.path == LOCK_PATH:
                    self._reply(stub.lock_status, stub.lock_body.encode())
                else:
                    self._reply(404)

            def do_PUT(self):
                self._record()
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                conditional = self.headers.get("If-None-Match") == "*"
                if stub.mode == "enforcing" and conditional and self.path in stub.objects:
                    self._reply(412)
                    return
                stub.objects[self.path] = body
                self._reply(200)

            def do_DELETE(self):
                self._record()
                stub.objects.pop(self.path, None)
                self._reply(204)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run(args: list[str], port: int | None, extra_env: dict[str, str] | None = None,
         creds: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_LOCK_", "GROVE_"))}
    if creds:
        env.update(AWS_ACCESS_KEY_ID="AKIATEST", AWS_SECRET_ACCESS_KEY="secrettest")
    env.update(GROVE_SPACES_SCHEME="http", GROVE_SPACES_HOST=f"127.0.0.1:{port or 9}")
    env.update(extra_env or {})
    return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True,
                          text=True, timeout=60)


def _free_port() -> int:
    """A port with nothing listening (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _guard(stub: _Stub, **env) -> subprocess.CompletedProcess:
    return _run(["guard", STATE_KEY], stub.port, env)


def test_guard_no_lock_proceeds():
    stub = _Stub("ignoring", lock_status=404)
    try:
        r = _guard(stub)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "no lockfile present" in r.stdout, r.stdout
    finally:
        stub.close()


def test_guard_held_lock_refuses():
    holder = '{"ID":"abc","Who":"josh@mac","Operation":"OperationTypeApply"}'
    stub = _Stub("ignoring", lock_status=200, lock_body=holder)
    try:
        r = _guard(stub)
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert "ALREADY HELD" in r.stdout, r.stdout
        assert "josh@mac" in r.stdout, "the lock holder must be shown to the operator"
    finally:
        stub.close()


def test_guard_override():
    stub = _Stub("ignoring", lock_status=200, lock_body="{}")
    try:
        r = _guard(stub, TF_LOCK_GUARD_OFF="1")
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "overriding" in r.stderr, r.stderr
    finally:
        stub.close()


def test_guard_inconclusive_proceeds():
    for status in (500, 503, 403):
        stub = _Stub("ignoring", lock_status=status)
        try:
            r = _guard(stub)
            assert r.returncode == 0, (status, r.returncode, r.stdout, r.stderr)
            assert "inconclusive" in r.stdout, (status, r.stdout)
        finally:
            stub.close()


def test_guard_unreachable_proceeds():
    r = _run(["guard", STATE_KEY], _free_port())
    assert r.returncode == 0, (
        "an unreachable backend must not abort train-up/teardown",
        r.returncode, r.stdout, r.stderr,
    )
    assert "inconclusive" in r.stdout and "HTTP 000" in r.stdout, r.stdout
    assert "Traceback" not in r.stderr, r.stderr


def test_guard_reads_the_right_key():
    stub = _Stub("ignoring", lock_status=404)
    try:
        _guard(stub)
        gets = [(p, h) for m, p, h in stub.requests if m == "GET"]
        assert [p for p, _ in gets] == [LOCK_PATH], stub.requests
        auth = gets[0][1].get("authorization", "")
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/"), auth
        assert "x-amz-content-sha256" in gets[0][1], gets[0][1]
    finally:
        stub.close()


def test_guard_needs_creds():
    r = _run(["guard", STATE_KEY], _free_port(), creds=False)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "AWS_ACCESS_KEY_ID" in r.stderr, r.stderr


def test_probe_enforcing_passes():
    stub = _Stub("enforcing")
    try:
        r = _run(["probe"], stub.port)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "HTTP 412" in r.stdout and "PASS" in r.stdout, r.stdout
    finally:
        stub.close()


def test_probe_ignoring_fails():
    stub = _Stub("ignoring")
    try:
        r = _run(["probe"], stub.port)
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert "FAIL" in r.stdout, r.stdout
        puts = [h for m, _p, h in stub.requests if m == "PUT"]
        assert len(puts) == 2, stub.requests
        assert all(h.get("if-none-match") == "*" for h in puts), puts
        # The probe cleans up after itself.
        assert not stub.objects, stub.objects
    finally:
        stub.close()


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
