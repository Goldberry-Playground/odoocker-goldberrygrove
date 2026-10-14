#!/usr/bin/env python3
"""Regression tests for scripts/qa-health-sweep.sh -- the parked-between-trains rule.

The release train tears QA compute down every Thursday and rebuilds it the next
train Monday, so for most of every fortnight there is nothing to probe. Before
this rule the sweep reported DEGRADED every 15 min for the whole gap
(2026-10-08 -> 10-19), keeping main's QA Health check permanently red, which
hides a real outage behind a known one.

The real script runs against a .invalid zone (every host -> 000, no network)
and a local stub DO API (QA_HEALTH_DO_API). QA_HEALTH_NOW pins the clock.
Anchor = Train #1 Monday 2026-09-21; even week index = train week.

  parked-off-week            all down + 0 apps, off week          -> exit 0, parked
  parked-train-week-friday   all down + 0 apps, train-week Fri    -> exit 0, parked
  parked-edge-monday         all down + 0 apps, train Monday      -> exit 0, parked
  parked-edge-thursday       all down + 0 apps, teardown Thursday -> exit 0, parked
  absent-in-live-window      all down + 0 apps, train Tuesday     -> exit 1, live-window FAIL
  partial-apps-not-parked    all down + 2 apps, off week          -> exit 1, DEGRADED
  no-token-not-parked        all down, no DO token, off week      -> exit 1, DEGRADED
  stuck-app-not-parked       all down + 4 apps (1 ERROR), off week -> exit 1, DEGRADED

    python3 scripts/test_qa_health_sweep.py
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "qa-health-sweep.sh")

APPS: list[dict] = []


class _DOStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        body = json.dumps({"apps": APPS}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def _app(name: str, phase: str = "ACTIVE") -> dict:
    return {"spec": {"name": name}, "active_deployment": {"phase": phase}}


def _epoch(day: str) -> str:
    d = dt.datetime.fromisoformat(day).replace(hour=12, tzinfo=dt.timezone.utc)
    return str(int(d.timestamp()))


# Anchor Mon 2026-09-21 = week 0 (train). Week 3 = off, week 4 = train.
OFF_WEEK_TUE = "2026-10-13"   # week 3, Tue
TRAIN_MON = "2026-10-19"      # week 4, Mon (train-up)
TRAIN_TUE = "2026-10-20"      # week 4, Tue (live)
TRAIN_THU = "2026-10-22"      # week 4, Thu (teardown)
TRAIN_FRI = "2026-10-23"      # week 4, Fri (off)


def run(server_port: int, day: str, apps: list[dict], token: bool = True):
    APPS[:] = apps
    env = {
        "PATH": os.environ["PATH"],
        "QA_ZONE": "qa-health-test.invalid",
        "QA_HEALTH_DO_API": f"http://127.0.0.1:{server_port}",
        "QA_HEALTH_NOW": _epoch(day),
    }
    if token:
        env["DIGITALOCEAN_TOKEN"] = "stub-token"
    p = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _DOStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    four = [_app(f"grove-{n}-qa") for n in ("hub", "goldberry", "ggg", "nursery")]

    cases = [
        ("parked-off-week", OFF_WEEK_TUE, [], True, 0, "RESULT: parked"),
        ("parked-train-week-friday", TRAIN_FRI, [], True, 0, "RESULT: parked"),
        ("parked-edge-monday", TRAIN_MON, [], True, 0, "RESULT: parked"),
        ("parked-edge-thursday", TRAIN_THU, [], True, 0, "RESULT: parked"),
        ("absent-in-live-window", TRAIN_TUE, [], True, 1, "absent during the train's live window"),
        ("partial-apps-not-parked", OFF_WEEK_TUE, four[:2], True, 1, "RESULT: DEGRADED"),
        ("no-token-not-parked", OFF_WEEK_TUE, [], False, 1, "RESULT: DEGRADED"),
        ("stuck-app-not-parked", OFF_WEEK_TUE, four[:3] + [_app("grove-nursery-qa", "ERROR")], True, 1, "RESULT: DEGRADED"),
    ]

    failed = 0
    for name, day, apps, token, want_rc, want_text in cases:
        rc, out = run(port, day, apps, token)
        ok = rc == want_rc and want_text in out
        print(f"{'ok  ' if ok else 'FAIL'} {name}: rc={rc} (want {want_rc})")
        if not ok:
            failed += 1
            print("     expected to find:", want_text)
            print("     " + out.replace("\n", "\n     "))

    srv.shutdown()
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
