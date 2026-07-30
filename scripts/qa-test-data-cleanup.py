#!/usr/bin/env python3
"""
qa-test-data-cleanup — tear QA test data back down to a clean, reproducible
pre-freeze state.

WHY THIS EXISTS
---------------
QA (qa.gatheringatthegrove.com) has been the Grove *system of record* for real
order/inventory data since 2026-07-09. QA also accumulates TEST data over a
release cycle: synthetic-monitoring canary orders, checkout/cart journey orders,
the SYNTHETIC-CANARY product, and any partners/attachments seeded while QAing.
Before a freeze we want QA back in a clean, reproducible state — but a blunt
"delete recent orders" sweep would eat REAL revenue data. So this teardown is
*surgical*: it only ever removes records that are PROVABLY not real customer
data, keyed on markers a real customer can never have.

THE ONE SAFE SELECTOR
---------------------
RFC 2606 / RFC 6761 permanently reserve the `.invalid`, `.test`, `.example` and
`.localhost` TLDs (and the `example.com/net/org` domains) for testing — the DNS
root will never delegate them, so no real customer can ever own an address
there. Every synthetic/test fixture in this repo already uses one
(`synthetic-canary@grove.invalid`; see synthetic/canary.py + the journeys). That
reserved-domain email is therefore a zero-false-positive marker for "test data":
any sale.order / res.partner hanging off such an address is test data and only
test data. We delete on THAT, plus the well-known SYNTHETIC-CANARY product code.
We NEVER key on dates, amounts, state, or "looks like a test" heuristics.

HOW IT DELETES
--------------
Through Odoo's ORM over XML-RPC (same client shape as synthetic/canary.py) —
never raw SQL. The ORM respects foreign-key cascades, access rules, and record
rules, so a delete that would corrupt referential integrity is refused by Odoo
instead of silently orphaning rows. Deletes run children-before-parents
(orders → partners → products) so unlink() isn't blocked by live references.

SAFETY RAILS
------------
  * DRY-RUN BY DEFAULT. Prints exactly what it *would* remove and exits without
    touching anything. You must pass --apply to delete.
  * TARGET GUARD. Refuses to run unless the XML-RPC host OR the DB name looks
    like QA/sandbox/staging, so a mis-set env can't point this at prod. Prod and
    QA both run a DB literally named `odoo`, so the QA *host*
    (odoo.qa.gatheringatthegrove.com) is the real signal. Override intentionally
    with --allow-nonqa-db (loud warning).
  * IDEMPOTENT. Selectors are stable; a second --apply run finds nothing and
    reports 0 removed. Safe to run twice, safe to run after a partial run.
  * The SYNTHETIC-CANARY *product* is a persistent monitoring fixture (setup-
    monitoring.py re-seeds it), so it is KEPT by default. Pass
    --include-canary-product to also remove it (the next monitoring cycle will
    re-create it).
  * Anonymous website "abandoned cart" draft orders are INDISTINGUISHABLE from a
    real shopper's abandoned cart, so they are only ever REPORTED, never
    deleted — Odoo's own sale-order cron expires them.

ENTRYPOINT / CREDENTIALS
------------------------
Prefer the documented wrapper `scripts/qa-test-data-cleanup.sh`, which resolves
credentials from 1Password via `op run` and passes flags through. Direct use:

    ODOO_XMLRPC_URL=https://odoo.qa.gatheringatthegrove.com \
    ODOO_DB=<qa-db> ODOO_LOGIN=<user> ODOO_API_KEY=<key> \
    python3 scripts/qa-test-data-cleanup.py            # dry-run (safe)
    python3 scripts/qa-test-data-cleanup.py --apply    # actually delete

Env:
  ODOO_XMLRPC_URL   default http://odoo:8069 (on-box); use the public QA URL off-box
  ODOO_DB           QA database name (guarded — see --allow-nonqa-db)
  ODOO_LOGIN        Odoo login for a least-privilege user
  ODOO_API_KEY      that user's API key (SYNTHETIC_ODOO_API_KEY accepted as a fallback)

Exit codes: 0 = success (dry-run or apply); 1 = connection/auth/guard failure.
Stdlib only (xmlrpc.client, argparse, json). All progress logs go to stderr;
--json emits a machine-readable summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xmlrpc.client

# Product code of the shared synthetic-monitoring canary (synthetic/canary.py).
CANARY_CODE = "SYNTHETIC-CANARY"

# RFC 2606 (invalid/test/example/localhost) + RFC 6761 reserved test names, and
# the example.{com,net,org} second-level domains. A real, deliverable customer
# email can never live under any of these, so they are zero-false-positive
# markers for test data. Matched case-insensitively against the email SUFFIX.
RESERVED_TEST_SUFFIXES = (
    ".invalid",
    ".test",
    ".example",
    ".localhost",
    "@example.com",
    "@example.net",
    "@example.org",
)


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


# ── pure selectors (unit-tested in synthetic/test_qa_cleanup.py) ──────────────

def is_test_email(email: object) -> bool:
    """True iff `email` is at an RFC-reserved test domain (never a real customer)."""
    if not isinstance(email, str):
        return False
    e = email.strip().lower()
    if not e or "@" not in e:
        return False
    return any(e.endswith(suffix) for suffix in RESERVED_TEST_SUFFIXES)


def test_partner_email_domain() -> list:
    """Odoo domain (OR of 'email =like' clauses) selecting reserved-test-domain partners."""
    clauses: list = ["|"] * (len(RESERVED_TEST_SUFFIXES) - 1)
    for suffix in RESERVED_TEST_SUFFIXES:
        # '=like' is case-insensitive in Odoo; '%'+suffix matches the address tail.
        clauses.append(["email", "=like", f"%{suffix}"])
    return clauses


# QA/sandbox/staging markers. Prod and QA both run a DB literally named `odoo`,
# so the DB name alone can't tell them apart — the reliable signal is the
# hostname in the XML-RPC URL (odoo.qa.… vs odoo.…). A marker in EITHER qualifies.
QA_MARKERS = ("qa", "sandbox", "staging", "sbx", "preview")


def is_qa_db_name(db: str) -> bool:
    """Heuristic: does this DB name look like a QA/sandbox/staging DB (not prod)?"""
    return any(tok in (db or "").lower() for tok in QA_MARKERS)


def is_qa_target(url: str, db: str) -> bool:
    """Guard: does the target (XML-RPC host OR DB name) look like QA — not prod?

    The real QA DB is named `odoo` (same as prod), so `is_qa_db_name` is False
    for it; the host `odoo.qa.gatheringatthegrove.com` is what distinguishes QA
    from prod. A bare on-box `http://odoo:8069` carries no marker and is
    intentionally refused, so on-box use must pass --allow-nonqa-db explicitly.
    """
    return is_qa_db_name(db) or any(tok in (url or "").lower() for tok in QA_MARKERS)


# ── XML-RPC client (mirrors synthetic/canary.py; mockable in tests) ───────────

class OdooClient:
    """Thin authenticated Odoo XML-RPC client."""

    def __init__(self, url: str, db: str, login: str, key: str):
        self.db = db
        self.key = key
        common = xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")
        self.uid = common.authenticate(db, login, key, {})
        if not self.uid:
            raise RuntimeError("Odoo XML-RPC authentication failed (check ODOO_DB/LOGIN/API_KEY)")
        self.models = xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/object")

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, args, kwargs or {})


# ── cleanup operations (take any object exposing .call(...) — real or fake) ────

def _search(client, model: str, domain: list, **kw) -> list:
    return client.call(model, "search", [domain], kw)


def _read(client, model: str, ids: list, fields: list) -> list:
    if not ids:
        return []
    return client.call(model, "read", [ids, fields])


def plan(client) -> dict:
    """Enumerate every test-data target WITHOUT deleting. Returns a structured plan."""
    # 1. Partners at reserved test domains.
    partner_ids = _search(client, "res.partner", test_partner_email_domain())
    partners = _read(client, "res.partner", partner_ids, ["id", "name", "email"])

    # 2. sale.orders owned by those partners (ANY state — a reserved-TLD email
    #    cannot be a real confirmed sale). Belt-and-suspenders: also match the
    #    commercial email directly via the related field.
    order_domain: list = []
    if partner_ids:
        order_domain = ["|", ["partner_id", "in", partner_ids],
                        ["partner_id.email", "=like", "%.invalid"]]
    else:
        order_domain = [["partner_id.email", "=like", "%.invalid"]]
    order_ids = _search(client, "sale.order", order_domain)
    orders = _read(client, "sale.order", order_ids, ["id", "name", "state", "amount_total"])

    # 3. SYNTHETIC-CANARY product (reported always; removed only with the flag).
    prod_tmpl_ids = _search(client, "product.template", [["default_code", "=", CANARY_CODE]])
    products = _read(client, "product.template", prod_tmpl_ids, ["id", "name", "default_code"])

    # 4. Anonymous website draft carts — REPORT ONLY (can't tell from a real one).
    anon_cart_ids = _search(
        client, "sale.order",
        [["state", "=", "draft"], ["website_id", "!=", False],
         ["partner_id", "not in", partner_ids or [0]]],
    )

    return {
        "test_partners": partners,
        "test_orders": orders,
        "canary_products": products,
        "anon_draft_carts": len(anon_cart_ids),
    }


def apply_cleanup(client, plan_data: dict, include_canary_product: bool) -> dict:
    """Delete the planned test data, children-before-parents. Returns per-model counts."""
    removed = {"orders": 0, "partners": 0, "products": 0}

    order_ids = [o["id"] for o in plan_data["test_orders"]]
    if order_ids:
        client.call("sale.order", "unlink", [order_ids])
        removed["orders"] = len(order_ids)

    # Partners only after their orders are gone, so unlink isn't FK-blocked. Any
    # partner the ORM still refuses (other live references) is left in place and
    # surfaced by the next dry-run — no forced/cascading accounting deletes.
    partner_ids = [p["id"] for p in plan_data["test_partners"]]
    if partner_ids:
        removed["partners"] = _safe_unlink(client, "res.partner", partner_ids)

    if include_canary_product:
        prod_ids = [p["id"] for p in plan_data["canary_products"]]
        if prod_ids:
            removed["products"] = _safe_unlink(client, "product.template", prod_ids)

    return removed


def _safe_unlink(client, model: str, ids: list) -> int:
    """Unlink ids; on a blocking reference fall back to per-id and skip the stuck ones."""
    try:
        client.call(model, "unlink", [ids])
        return len(ids)
    except xmlrpc.client.Fault as exc:
        _log(f"  {model}: bulk unlink blocked ({exc.faultString.splitlines()[0]}); retrying per-record")
        ok = 0
        for rid in ids:
            try:
                client.call(model, "unlink", [[rid]])
                ok += 1
            except xmlrpc.client.Fault as inner:
                _log(f"  {model}#{rid}: kept (still referenced: {inner.faultString.splitlines()[0]})")
        return ok


# ── reporting ─────────────────────────────────────────────────────────────────

def render_human(plan_data: dict, removed: dict | None, include_canary_product: bool) -> None:
    _log("== QA test-data cleanup ==")
    p, o, prod = plan_data["test_partners"], plan_data["test_orders"], plan_data["canary_products"]
    _log(f"  test partners (reserved-domain email): {len(p)}")
    for row in p[:10]:
        _log(f"      partner#{row['id']}  {row.get('email')!r}  {row.get('name')!r}")
    _log(f"  test sale.orders:                       {len(o)}")
    for row in o[:10]:
        _log(f"      order#{row['id']}  {row.get('name')}  state={row.get('state')}  total={row.get('amount_total')}")
    verb = "WILL REMOVE" if include_canary_product else "kept (fixture; use --include-canary-product)"
    _log(f"  SYNTHETIC-CANARY product:               {len(prod)}  [{verb}]")
    _log(f"  anonymous draft website carts:          {plan_data['anon_draft_carts']}  [report only — Odoo expires these]")
    if removed is None:
        _log("  MODE: dry-run — nothing deleted. Re-run with --apply to remove.")
    else:
        _log(f"  MODE: apply — removed orders={removed['orders']} partners={removed['partners']} products={removed['products']}")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Tear down QA test data to a clean pre-freeze state.")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run report only)")
    ap.add_argument("--include-canary-product", action="store_true",
                    help="also remove the SYNTHETIC-CANARY product (monitoring re-seeds it)")
    ap.add_argument("--allow-nonqa-db", action="store_true",
                    help="bypass the QA-DB-name guard (dangerous; requires intent)")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable summary to stdout")
    return ap


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv[1:])

    url = os.environ.get("ODOO_XMLRPC_URL", "http://odoo:8069")
    db = os.environ.get("ODOO_DB", "")
    login = os.environ.get("ODOO_LOGIN", "")
    key = os.environ.get("ODOO_API_KEY") or os.environ.get("SYNTHETIC_ODOO_API_KEY", "")

    missing = [n for n, v in (("ODOO_DB", db), ("ODOO_LOGIN", login), ("ODOO_API_KEY", key)) if not v]
    if missing:
        _log(f"ERROR: missing required env {missing}")
        return 1

    if not is_qa_target(url, db) and not args.allow_nonqa_db:
        _log(f"ERROR: neither ODOO_XMLRPC_URL={url!r} nor ODOO_DB={db!r} looks like QA/sandbox/staging.")
        _log("       (prod and QA share the DB name 'odoo' — the QA host is the signal.)")
        _log("       Refusing to run so a mis-set env can't hit prod. Pass --allow-nonqa-db to override.")
        return 1
    if not is_qa_target(url, db):
        _log(f"WARNING: --allow-nonqa-db set; operating on non-QA target url={url!r} db={db!r}.")

    try:
        client = OdooClient(url, db, login, key)
    except (OSError, xmlrpc.client.Fault, RuntimeError) as exc:
        _log(f"ERROR: cannot connect to Odoo at {url}: {exc}")
        return 1

    plan_data = plan(client)
    removed = None
    if args.apply:
        removed = apply_cleanup(client, plan_data, args.include_canary_product)

    render_human(plan_data, removed, args.include_canary_product)

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply,
            "counts": {
                "test_partners": len(plan_data["test_partners"]),
                "test_orders": len(plan_data["test_orders"]),
                "canary_products": len(plan_data["canary_products"]),
                "anon_draft_carts": plan_data["anon_draft_carts"],
            },
            "removed": removed or {},
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
