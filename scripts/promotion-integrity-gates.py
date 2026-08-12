#!/usr/bin/env python3
"""
promotion-integrity-gates — read-only data-integrity gates for a QA→prod Odoo
promotion (GOL-1329).

WHY THIS EXISTS
---------------
QA Odoo has been the Grove *system of record* for real orders/sales since
2026-07-09. When that data is promoted to prod (scripts/promote-db.sh restore +
scripts/prod-reconfig.py), "the restore ran without error" is NOT proof the data
is intact and safe to sell against. A dropped sequence, a reset invoice counter,
or a mangled tax line does not throw at restore time — it surfaces as a
duplicate order number, an out-of-order invoice, or a mis-charged customer days
later. These gates turn those silent risks into an explicit, evidence-producing
go/no-go BEFORE the freeze thaws and traffic returns.

Every gate is READ-ONLY (search/read/search_count only — it never writes). Run
it against the restored target (scratch during rehearsal, prod during the real
cutover). It is also the tool that produces the pre-vs-post row/order counts the
rehearsal evidence requires.

THE GATES
---------
  1. order-sequence continuity — the sale.order ir.sequence's next number is
     strictly greater than the highest existing order's numeric suffix, so the
     next real order can't collide with a promoted one.
  2. invoice-numbering continuity — same check for posted customer invoices
     (account.move, move_type=out_invoice): the invoice sequence won't re-issue
     a number already used. (Invoice numbers are legally sequential — a
     collision or gap is an accounting defect.)
  3. WV-nexus tax spot-check — Grove has sales-tax nexus in WV only (GOL-1021).
     Sample confirmed orders: every order shipping to WV must carry a non-zero
     tax amount; a sample shipping OUTSIDE WV must carry zero. Catches a tax
     configuration that didn't survive the promotion.
  4. promotion completeness — compare live order/partner/invoice counts against a
     baseline manifest captured on the SOURCE before the freeze (--baseline).
     A post-count LOWER than baseline means rows were lost in transit.

Gates 1–3 are HARD by default (a failure exits non-zero → do not cut over).
Gate 4 requires --baseline; without it, it is reported as SKIPPED (not failed),
so the tool is still useful for a standalone integrity read.

USAGE
-----
    # capture the SOURCE baseline (on/against QA, before the freeze):
    ODOO_XMLRPC_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=… \
      ODOO_LOGIN=… ODOO_API_KEY=… \
      python3 scripts/promotion-integrity-gates.py --emit-baseline > baseline.json

    # run the gates against the restored TARGET (scratch/prod):
    ODOO_XMLRPC_URL=… ODOO_DB=… ODOO_LOGIN=… ODOO_API_KEY=… \
      python3 scripts/promotion-integrity-gates.py --baseline baseline.json --json

Env: ODOO_XMLRPC_URL, ODOO_DB, ODOO_LOGIN, ODOO_API_KEY (as the other scripts).
Exit codes: 0 all hard gates pass; 1 connection/auth error; 2 a hard gate failed.
Stdlib only. Progress → stderr; --json / --emit-baseline → stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xmlrpc.client

WV_STATE_CODES = ("WV",)  # West Virginia — Grove's only sales-tax nexus (GOL-1021)


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


# ── helpers (pure — unit-tested against a fake client) ────────────────────────

_TRAILING_NUM = re.compile(r"(\d+)\s*$")


def trailing_number(name: object) -> int | None:
    """Extract the trailing integer of an order/invoice name (e.g. 'S00042'→42,
    'INV/2026/0007'→7). None if there is no trailing digit run."""
    if not isinstance(name, str):
        return None
    m = _TRAILING_NUM.search(name)
    return int(m.group(1)) if m else None


def max_trailing_number(names: list) -> int:
    """Highest trailing number across names (0 if none parse)."""
    nums = [n for n in (trailing_number(x) for x in names) if n is not None]
    return max(nums) if nums else 0


def sequence_next_number(client, code: str) -> int | None:
    """The 'number_next_actual' of the ir.sequence with the given code (None if
    absent). This is what Odoo will assign to the NEXT record."""
    rows = client.call("ir.sequence", "search_read",
                       [[["code", "=", code]]], {"fields": ["number_next_actual"], "limit": 1})
    return rows[0]["number_next_actual"] if rows else None


# ── gates ─────────────────────────────────────────────────────────────────────

def gate_sequence_continuity(client, model: str, seq_code: str, name_field: str,
                             domain: list, label: str) -> dict:
    """A sequence gate: next-number must exceed the highest existing record's
    trailing number, else the next issued record collides with a promoted one.

    When there are no records AND no sequence (e.g. invoices pre-launch), the
    gate is skipped — there is nothing to collide with. A missing sequence with
    existing records is still a hard FAIL.
    """
    names = [r[name_field] for r in
             client.call(model, "search_read", [domain], {"fields": [name_field]})]
    highest = max_trailing_number(names)
    nxt = sequence_next_number(client, seq_code)
    if nxt is None:
        if not names:
            # No records, no sequence — nothing to check; skip.
            return {"label": label, "ok": True, "skipped": True,
                    "detail": f"no {model} records and ir.sequence code={seq_code!r} not found — skip (pre-launch)",
                    "highest_existing": 0, "records": 0}
        return {"label": label, "ok": False,
                "detail": f"ir.sequence code={seq_code!r} not found but {len(names)} records exist — cannot verify continuity",
                "highest_existing": highest, "records": len(names)}
    ok = nxt > highest
    return {"label": label, "ok": ok,
            "detail": f"next={nxt} highest_existing={highest} records={len(names)}"
                      + ("" if ok else "  ← next would collide/regress"),
            "highest_existing": highest, "next": nxt, "records": len(names)}


def _order_ship_state_code(order: dict) -> str | None:
    """The partner_shipping's state code from a sale.order read that included
    'partner_shipping_id' via read_group-style related fields is awkward over
    XML-RPC; callers pass the resolved code. This helper is here for the fake."""
    return order.get("_ship_state_code")


def gate_wv_tax_spotcheck(client, sample: int = 25) -> dict:
    """Sample confirmed orders; WV-ship orders must have tax>0, non-WV must be 0.

    Reads partner_shipping_id → res.partner.state_id → res.country.state.code to
    classify each order's destination, then checks amount_tax sign. A single
    misclassified charge is a real customer-money defect (GOL-1021)."""
    order_ids = client.call("sale.order", "search",
                            [[["state", "in", ["sale", "done"]]]], {"limit": sample, "order": "id desc"})
    orders = client.call("sale.order", "read",
                         [order_ids], {"fields": ["name", "amount_tax", "partner_shipping_id"]})
    # resolve shipping-state codes in one batch
    ship_ids = [o["partner_shipping_id"][0] for o in orders if o.get("partner_shipping_id")]
    partners = {p["id"]: p for p in client.call("res.partner", "read",
                [ship_ids], {"fields": ["state_id"]})} if ship_ids else {}
    state_ids = [p["state_id"][0] for p in partners.values() if p.get("state_id")]
    states = {s["id"]: s.get("code") for s in client.call("res.country.state", "read",
              [state_ids], {"fields": ["code"]})} if state_ids else {}

    violations = []
    checked = 0
    for o in orders:
        pid = o["partner_shipping_id"][0] if o.get("partner_shipping_id") else None
        sid = partners.get(pid, {}).get("state_id") if pid else None
        code = states.get(sid[0]) if sid else None
        if code is None:
            continue  # unknown destination — cannot classify, skip (don't false-fail)
        checked += 1
        tax = o.get("amount_tax") or 0.0
        is_wv = code in WV_STATE_CODES
        if is_wv and tax <= 0:
            violations.append(f"{o['name']}: WV ship but tax={tax}")
        if not is_wv and tax > 0:
            violations.append(f"{o['name']}: non-WV ({code}) ship but tax={tax}")
    ok = not violations
    detail = f"checked={checked} violations={len(violations)}"
    if violations:
        detail += " :: " + "; ".join(violations[:5])
    return {"label": "WV-nexus tax spot-check", "ok": ok, "detail": detail}


def collect_counts(client) -> dict:
    """The completeness census — cheap search_counts used for baseline + compare."""
    return {
        "sale_orders": client.call("sale.order", "search_count", [[]]),
        "confirmed_orders": client.call("sale.order", "search_count", [[["state", "in", ["sale", "done"]]]]),
        "partners": client.call("res.partner", "search_count", [[]]),
        "posted_invoices": client.call("account.move", "search_count",
                                       [[["move_type", "=", "out_invoice"], ["state", "=", "posted"]]]),
    }


def gate_completeness(counts: dict, baseline: dict | None) -> dict:
    if baseline is None:
        return {"label": "promotion completeness", "ok": True, "skipped": True,
                "detail": "no --baseline supplied — completeness gate SKIPPED", "counts": counts}
    regressions = {k: (baseline.get(k), counts.get(k))
                   for k in counts if counts.get(k, 0) < baseline.get(k, 0)}
    ok = not regressions
    detail = f"counts={counts} baseline={baseline}"
    if regressions:
        detail += f"  ← rows LOST: {regressions}"
    return {"label": "promotion completeness", "ok": ok, "detail": detail,
            "counts": counts, "baseline": baseline}


# ── driver ────────────────────────────────────────────────────────────────────

def run_gates(client, baseline: dict | None) -> list[dict]:
    results = [
        gate_sequence_continuity(client, "sale.order", "sale.order", "name",
                                 [], "order-sequence continuity"),
        gate_sequence_continuity(client, "account.move", "account.move.out_invoice", "name",
                                 [["move_type", "=", "out_invoice"], ["state", "=", "posted"]],
                                 "invoice-numbering continuity"),
        gate_wv_tax_spotcheck(client),
        gate_completeness(collect_counts(client), baseline),
    ]
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Read-only QA→prod promotion integrity gates.")
    ap.add_argument("--baseline", help="path to a baseline census JSON (from --emit-baseline on the source)")
    ap.add_argument("--emit-baseline", action="store_true",
                    help="print a completeness census (counts) to stdout and exit (run on the SOURCE)")
    ap.add_argument("--json", action="store_true", help="machine-readable results to stdout")
    return ap


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv[1:])
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

    if args.emit_baseline:
        census = collect_counts(client)
        _log(f"== baseline census (source db={db}) ==  {census}")
        print(json.dumps(census, indent=2))
        return 0

    baseline = None
    if args.baseline:
        with open(args.baseline, "r", encoding="utf-8") as fh:
            baseline = json.load(fh)

    _log(f"== promotion integrity gates ==  db={db}  url={url}")
    results = run_gates(client, baseline)
    failures = 0
    for r in results:
        if r.get("skipped"):
            _log(f"  gate[{r['label']}] SKIP: {r['detail']}")
            continue
        verdict = "PASS" if r["ok"] else "FAIL"
        _log(f"  gate[{r['label']}] {verdict}: {r['detail']}")
        if not r["ok"]:
            failures += 1

    if args.json:
        print(json.dumps({"db": db, "url": url, "results": results, "failures": failures}, indent=2))

    if failures:
        _log(f"❌ {failures} integrity gate(s) FAILED — data not safe to serve. DO NOT cut over.")
        return 2
    _log("✅ all integrity gates pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
