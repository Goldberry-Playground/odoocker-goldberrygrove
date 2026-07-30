#!/usr/bin/env python3
"""Unit tests for qa-test-data-cleanup — the surgical QA teardown.

Runs without a network or a real Odoo — an in-memory FakeOdoo implements just
enough of the XML-RPC `search`/`read`/`unlink` surface to prove the two things
that matter for a system-of-record env:

  1. SURGICAL: real customer orders/partners (deliverable emails) are NEVER
     touched; only reserved-test-domain (RFC 2606/6761) records are removed.
  2. IDEMPOTENT: a second --apply run over the same DB removes 0 records.

    python3 scripts/test_qa_test_data_cleanup.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "qa_test_data_cleanup", os.path.join(_HERE, "qa-test-data-cleanup.py")
)
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


class FakeOdoo:
    """Minimal in-memory Odoo supporting the search/read/unlink calls the script makes.

    Records: {model: {id: {field: value}}}. Supports the exact domain leaf
    operators the script emits: '=', '=like', 'in', 'not in', '!='. res.partner
    is read-through for the 'partner_id.email' related-field leaf on sale.order.
    """

    def __init__(self, records: dict):
        self.records = {m: dict(rows) for m, rows in records.items()}

    # -- domain evaluation -----------------------------------------------------
    def _match_leaf(self, model, rec_id, rec, leaf):
        field, op, val = leaf
        if field == "partner_id.email":  # related field hop through res.partner
            pid = rec.get("partner_id")
            actual = self.records.get("res.partner", {}).get(pid, {}).get("email")
        elif field == "partner_id" and op in ("in", "not in"):
            actual = rec.get("partner_id")
        else:
            actual = rec.get(field)
        if op == "=":
            return actual == val
        if op == "!=":
            return actual != val
        if op == "in":
            return actual in val
        if op == "not in":
            return actual not in val
        if op == "=like":  # case-insensitive, % wildcard tail (as Odoo does)
            if not isinstance(actual, str):
                return False
            pat = val.lower()
            a = actual.lower()
            if pat.startswith("%"):
                return a.endswith(pat[1:])
            return a == pat
        raise AssertionError(f"unsupported op {op!r}")

    def _eval(self, model, rec_id, rec, domain):
        # Prefix (Polish) notation: '|' / '&' operators, else leaf. Build a
        # postfix-free recursive evaluator over the token stream.
        tokens = list(domain)
        pos = 0

        def parse():
            nonlocal pos
            tok = tokens[pos]
            pos += 1
            if tok == "|":
                a = parse(); b = parse(); return a or b
            if tok == "&":
                a = parse(); b = parse(); return a and b
            return self._match_leaf(model, rec_id, rec, tok)

        result = True
        # Implicit AND across top-level terms. Call parse() unconditionally so
        # pos always advances — `result and parse()` would short-circuit once
        # result is False and never consume the rest, hanging the parser.
        while pos < len(tokens):
            term = parse()
            result = result and term
        return result

    # -- XML-RPC surface -------------------------------------------------------
    def call(self, model, method, args, kwargs=None):
        if method == "search":
            domain = args[0]
            return [rid for rid, rec in self.records.get(model, {}).items()
                    if self._eval(model, rid, rec, domain)]
        if method == "read":
            ids, fields = args[0], args[1]
            out = []
            for rid in ids:
                rec = self.records[model][rid]
                # id is authoritative — set it last so a "id" in `fields` (which
                # the records dict doesn't store) can't clobber it with None.
                out.append({**{f: rec.get(f) for f in fields}, "id": rid})
            return out
        if method == "unlink":
            for rid in args[0]:
                self.records[model].pop(rid, None)
            return True
        raise AssertionError(f"unsupported method {method!r}")


def _seed():
    """A DB mixing REAL customer data with test data seeded during QA."""
    return {
        "res.partner": {
            1: {"name": "Real Customer", "email": "alice@gmail.com"},
            2: {"name": "Synthetic Canary", "email": "synthetic-canary@grove.invalid"},
            3: {"name": "QA Tester", "email": "qa@example.com"},
            4: {"name": "Local Dev", "email": "dev@grove.test"},
        },
        "sale.order": {
            # REAL confirmed revenue — must survive.
            10: {"name": "SO-REAL-1", "partner_id": 1, "state": "sale", "amount_total": 149.0, "website_id": False},
            # Test orders off reserved-domain partners — must all go.
            11: {"name": "SO-CANARY", "partner_id": 2, "state": "draft", "amount_total": 0.0, "website_id": False},
            12: {"name": "SO-QA", "partner_id": 3, "state": "sale", "amount_total": 5.0, "website_id": False},
            # Anonymous abandoned website cart (real partner) — REPORT ONLY, keep.
            13: {"name": "SO-ANON", "partner_id": 1, "state": "draft", "amount_total": 12.0, "website_id": 7},
        },
        "product.template": {
            20: {"name": "Real Product", "default_code": "GG-APPLE"},
            21: {"name": "SYNTHETIC-CANARY (monitoring — do not sell)", "default_code": "SYNTHETIC-CANARY"},
        },
    }


# ── pure-selector tests ───────────────────────────────────────────────────────

def test_is_test_email() -> None:
    for good in ("a@grove.invalid", "x@example.com", "y@GROVE.TEST", "z@a.example", "q@h.localhost"):
        assert cleanup.is_test_email(good), good
    for real in ("alice@gmail.com", "bob@goldberrygrove.farm", "", None, "no-at-sign", "x@invalid.com"):
        assert not cleanup.is_test_email(real), real


def test_qa_db_guard() -> None:
    for ok in ("grove_qa", "qa", "sandbox_db", "staging", "grove_sbx", "preview_1"):
        assert cleanup.is_qa_db_name(ok), ok
    for prod in ("grove_prod", "production", "goldberry", "main"):
        assert not cleanup.is_qa_db_name(prod), prod


def test_qa_target_guard() -> None:
    # Real QA and prod both run a DB named 'odoo' — the HOST is what distinguishes
    # them, so the guard must accept the QA host even with a prod-shaped DB name.
    assert cleanup.is_qa_target("https://odoo.qa.gatheringatthegrove.com", "odoo")
    assert not cleanup.is_qa_target("https://odoo.gatheringatthegrove.com", "odoo")
    # A DB-name marker alone still qualifies (named sandbox/staging DBs).
    assert cleanup.is_qa_target("http://odoo:8069", "grove_sandbox")
    # Bare on-box default against a prod-shaped DB name is refused (needs override).
    assert not cleanup.is_qa_target("http://odoo:8069", "odoo")


# ── lifecycle tests ────────────────────────────────────────────────────────────

def test_plan_selects_only_test_data() -> None:
    fake = FakeOdoo(_seed())
    p = cleanup.plan(fake)
    assert {r["id"] for r in p["test_partners"]} == {2, 3, 4}          # real partner 1 excluded
    assert {r["id"] for r in p["test_orders"]} == {11, 12}             # SO-REAL(10) + SO-ANON(13) excluded
    assert {r["id"] for r in p["canary_products"]} == {21}
    assert p["anon_draft_carts"] == 1                                  # SO-ANON reported, not deleted


def test_apply_is_surgical_and_keeps_canary_product_by_default() -> None:
    fake = FakeOdoo(_seed())
    removed = cleanup.apply_cleanup(fake, cleanup.plan(fake), include_canary_product=False)
    assert removed == {"orders": 2, "partners": 3, "products": 0}
    # Real revenue + real partner + anon cart + both products survive.
    assert set(fake.records["sale.order"]) == {10, 13}
    assert set(fake.records["res.partner"]) == {1}
    assert set(fake.records["product.template"]) == {20, 21}


def test_include_canary_product_removes_the_fixture() -> None:
    fake = FakeOdoo(_seed())
    removed = cleanup.apply_cleanup(fake, cleanup.plan(fake), include_canary_product=True)
    assert removed["products"] == 1
    assert set(fake.records["product.template"]) == {20}              # real product only


def test_second_apply_is_idempotent() -> None:
    fake = FakeOdoo(_seed())
    cleanup.apply_cleanup(fake, cleanup.plan(fake), include_canary_product=True)
    # Re-plan + re-apply over the already-cleaned DB removes nothing.
    p2 = cleanup.plan(fake)
    assert p2["test_partners"] == [] and p2["test_orders"] == [] and p2["canary_products"] == []
    removed2 = cleanup.apply_cleanup(fake, p2, include_canary_product=True)
    assert removed2 == {"orders": 0, "partners": 0, "products": 0}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
    sys.exit(0)
