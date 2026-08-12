#!/usr/bin/env python3
"""
qa-reseed-guard — the hard block that stops a blind QA re-seed from destroying
un-promoted real orders (GOL-1329).

WHY THIS EXISTS
---------------
QA Odoo (qa.gatheringatthegrove.com) has been the Grove *system of record* for
REAL customer orders/sales since 2026-07-09. Any operation that re-initialises
or re-seeds that database — a fresh `odoo -i`, a demo-data reload, a "reset QA to
a clean state" script — would irreversibly delete that live revenue data. The
QA→prod promotion (docs/runbooks/qa-to-prod-data-promotion.md) is the ONLY safe
path to move it. This guard makes "did we already promote + verify this data to
prod?" a machine-checkable precondition of any reseed, so a reseed cannot fire
mid-promotion or before one ever happened.

THE MARKER
----------
A single ir.config_parameter on the QA DB:

    key   grove.promotion.verified
    value {"verified_at": "...", "dump_sha256": "...", "prod_confirmed_by": "...",
           "note": "..."}

It is SET only by the final, human-confirmed step of a successful prod promotion
(`set` subcommand — needs the dump sha256 that was actually restored + the human
who verified prod). Its PRESENCE means: this QA data has been promoted to prod
and prod was verified, so prod is now the system of record and QA may be reseeded.
Its ABSENCE means: QA still holds the only copy of real data → reseed is BLOCKED.

HOW A RESEED USES IT
--------------------
Any reseed / DB-reinit script MUST gate on this first and abort on non-zero:

    python3 scripts/qa-reseed-guard.py check || {
        echo "QA reseed BLOCKED — promote+verify to prod first (GOL-1329)"; exit 1; }
    # …only now proceed with the reseed…

SUBCOMMANDS
-----------
  check   (default) exit 0 if the marker exists (reseed ALLOWED), exit 3 if it
          does not (reseed BLOCKED). Read-only.
  set     write the marker. Requires --dump-sha256 and --confirmed-by. Run this
          as the final step of a verified prod promotion.
  clear   remove the marker (QA resumes being system-of-record for a new cycle).
          Requires --i-understand to prevent accidental re-arming of the block.

Env: ODOO_XMLRPC_URL, ODOO_DB, ODOO_LOGIN, ODOO_API_KEY (as the sibling scripts).
Exit codes: 0 ok / allowed; 1 connection/auth/usage error; 3 BLOCKED (check found
no marker). Stdlib only. Progress → stderr; --json summary → stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xmlrpc.client

MARKER_KEY = "grove.promotion.verified"


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


class OdooClient:
    def __init__(self, url: str, db: str, login: str, key: str):
        self.url, self.db, self.key = url.rstrip("/"), db, key
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, login, key, {})
        if not self.uid:
            raise RuntimeError("Odoo XML-RPC authentication failed (check ODOO_DB/LOGIN/API_KEY)")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, args, kwargs or {})


def read_marker(client) -> str | None:
    rows = client.call("ir.config_parameter", "search_read",
                       [[["key", "=", MARKER_KEY]]], {"fields": ["value"], "limit": 1})
    return rows[0]["value"] if rows else None


def set_marker(client, payload: dict) -> None:
    client.call("ir.config_parameter", "set_param", [MARKER_KEY, json.dumps(payload, sort_keys=True)])


def clear_marker(client) -> bool:
    ids = client.call("ir.config_parameter", "search", [[["key", "=", MARKER_KEY]]])
    if ids:
        client.call("ir.config_parameter", "unlink", [ids])
    return bool(ids)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="QA reseed hard-block guard (promotion-verified marker).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", help="exit 0 if reseed allowed, 3 if blocked (default)")
    s = sub.add_parser("set", help="write the promotion-verified marker")
    s.add_argument("--dump-sha256", required=True, help="sha256 of the promotion dump that was restored to prod")
    s.add_argument("--confirmed-by", required=True, help="human who verified prod after the promotion")
    s.add_argument("--verified-at", required=True, help="ISO timestamp of prod verification (pass it in explicitly)")
    s.add_argument("--note", default="", help="optional free-text note")
    c = sub.add_parser("clear", help="remove the marker (re-arm the block for a new cycle)")
    c.add_argument("--i-understand", action="store_true", required=False,
                   help="required acknowledgement that clearing re-blocks QA reseed")
    ap.add_argument("--json", action="store_true", help="machine-readable summary to stdout")
    return ap


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv[1:])
    cmd = args.cmd or "check"

    url = os.environ.get("ODOO_XMLRPC_URL", "http://odoo:8069")
    db, login = os.environ.get("ODOO_DB", ""), os.environ.get("ODOO_LOGIN", "")
    key = os.environ.get("ODOO_API_KEY") or os.environ.get("SYNTHETIC_ODOO_API_KEY", "")
    missing = [n for n, v in (("ODOO_DB", db), ("ODOO_LOGIN", login), ("ODOO_API_KEY", key)) if not v]
    if missing:
        _log(f"ERROR: missing required env {missing}")
        return 1

    try:
        client = OdooClient(url, db, login, key)
    except (OSError, xmlrpc.client.Fault, RuntimeError) as exc:
        _log(f"ERROR: cannot connect to Odoo at {url}: {exc}")
        return 1

    if cmd == "check":
        marker = read_marker(client)
        allowed = marker is not None
        if args.json:
            print(json.dumps({"marker_key": MARKER_KEY, "present": allowed, "value": marker}, indent=2))
        if allowed:
            _log(f"✅ reseed ALLOWED — promotion-verified marker present: {marker}")
            return 0
        _log("🚫 reseed BLOCKED — no grove.promotion.verified marker on this DB.")
        _log("   QA still holds un-promoted real data. Promote + verify to prod first")
        _log("   (docs/runbooks/qa-to-prod-data-promotion.md), then `qa-reseed-guard.py set`.")
        return 3

    if cmd == "set":
        payload = {"verified_at": args.verified_at, "dump_sha256": args.dump_sha256,
                   "prod_confirmed_by": args.confirmed_by, "note": args.note}
        set_marker(client, payload)
        _log(f"✅ marker set: {payload}")
        if args.json:
            print(json.dumps({"marker_key": MARKER_KEY, "present": True, "value": payload}, indent=2))
        return 0

    if cmd == "clear":
        if not args.i_understand:
            _log("ERROR: `clear` re-blocks QA reseed. Pass --i-understand to confirm intent.")
            return 1
        existed = clear_marker(client)
        _log(f"✅ marker cleared (existed={existed}) — QA reseed is now BLOCKED again.")
        if args.json:
            print(json.dumps({"marker_key": MARKER_KEY, "present": False, "cleared": existed}, indent=2))
        return 0

    _log(f"ERROR: unknown subcommand {cmd!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
