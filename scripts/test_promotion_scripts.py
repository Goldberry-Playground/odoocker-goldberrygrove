#!/usr/bin/env python3
"""Unit tests for the QA→prod promotion tooling (GOL-1329).

Runs with no network and no real Odoo — an in-memory FakeOdoo implements just
enough of the XML-RPC surface (search / search_read / search_count / read /
write / unlink / set_param) to prove the properties that matter for a
system-of-record promotion:

  prod-reconfig            env-ref resolution refuses to write empty secrets;
                           writes are idempotent; assertions PASS/FAIL correctly.
  promotion-integrity-gates  sequence continuity catches a would-be collision;
                           WV-nexus tax spot-check catches a mis-charged order;
                           completeness catches lost rows.
  qa-reseed-guard          BLOCK without marker, ALLOW with it, set/clear cycle.

    python3 scripts/test_promotion_scripts.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

reconfig = _load("prod_reconfig", "prod-reconfig.py")
gates = _load("promotion_integrity_gates", "promotion-integrity-gates.py")
guard = _load("qa_reseed_guard", "qa-reseed-guard.py")


# ── a small but honest fake Odoo ──────────────────────────────────────────────

def _like_to_regex(pattern: str) -> str:
    # SQL LIKE: % → .*, _ → . ; everything else literal.
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "^" + "".join(out) + "$"


class FakeOdoo:
    """Records: {model: {id: {field: value}}}. Supports the leaf operators the
    scripts emit. Many2one fields are stored as [id, name] pairs (Odoo shape)."""

    def __init__(self, records: dict):
        self.records = {m: dict(rows) for m, rows in records.items()}

    # -- domain evaluation --
    def _match_leaf(self, rec: dict, leaf) -> bool:
        field, op, val = leaf
        got = rec.get(field)
        if op == "=":
            return got == val
        if op == "!=":
            return got != val
        if op == "in":
            return got in val
        if op == "not in":
            return got not in val
        if op == ">":
            return (got or 0) > val
        if op == "<":
            return (got or 0) < val
        if op == ">=":
            return (got or 0) >= val
        if op in ("like", "=like"):
            return isinstance(got, str) and re.match(_like_to_regex(val), got) is not None
        if op in ("ilike", "=ilike"):
            return isinstance(got, str) and re.match(_like_to_regex(val), got, re.IGNORECASE) is not None
        raise AssertionError(f"fake: unsupported operator {op!r}")

    def _matches(self, rec: dict, domain: list) -> bool:
        # only flat AND domains (no '|'/'&' prefixes) are used by these scripts
        return all(self._match_leaf(rec, leaf) for leaf in domain if isinstance(leaf, list))

    def _search_ids(self, model: str, domain: list) -> list:
        rows = self.records.get(model, {})
        return [rid for rid, rec in rows.items() if self._matches(rec, domain)]

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        kwargs = kwargs or {}
        if method == "search":
            ids = self._search_ids(model, args[0])
            if kwargs.get("order") == "id desc":
                ids = sorted(ids, reverse=True)
            return ids[: kwargs["limit"]] if kwargs.get("limit") else ids
        if method == "search_count":
            return len(self._search_ids(model, args[0]))
        if method == "search_read":
            ids = self._search_ids(model, args[0])
            if kwargs.get("limit"):
                ids = ids[: kwargs["limit"]]
            fields = kwargs.get("fields")
            return [self._read_one(model, rid, fields) for rid in ids]
        if method == "read":
            ids = args[0]
            fields = (args[1] if len(args) > 1 else None) or kwargs.get("fields")
            return [self._read_one(model, rid, fields) for rid in ids]
        if method == "write":
            ids, values = args[0], args[1]
            for rid in ids:
                self.records[model][rid].update(values)
            return True
        if method == "unlink":
            for rid in args[0]:
                self.records.get(model, {}).pop(rid, None)
            return True
        if method == "set_param":  # ir.config_parameter.set_param(key, value)
            key, value = args[0], args[1]
            rows = self.records.setdefault("ir.config_parameter", {})
            for rec in rows.values():
                if rec.get("key") == key:
                    rec["value"] = value
                    return True
            new_id = (max(rows) + 1) if rows else 1
            rows[new_id] = {"key": key, "value": value}
            return True
        raise AssertionError(f"fake: unsupported method {model}.{method}")

    def _read_one(self, model: str, rid: int, fields):
        rec = dict(self.records[model][rid])
        rec["id"] = rid
        if fields:
            return {f: rec.get(f) for f in list(fields) + ["id"]}
        return rec


# ── test harness ──────────────────────────────────────────────────────────────

_failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ── prod-reconfig ─────────────────────────────────────────────────────────────

def test_reconfig_env_refs():
    print("prod-reconfig: env-ref resolution")
    env = {"A": "https://prod", "B": "live_key"}
    out = reconfig.resolve_env_refs({"url": "${A}", "nested": ["${B}", "lit"]}, env)
    check("resolves refs", out == {"url": "https://prod", "nested": ["live_key", "lit"]})
    raised = False
    try:
        reconfig.resolve_env_refs("${MISSING}", env)
    except KeyError:
        raised = True
    check("missing ref raises (never writes empty secret)", raised)
    raised = False
    try:
        reconfig.resolve_env_refs("${EMPTY}", {"EMPTY": ""})
    except KeyError:
        raised = True
    check("empty ref raises", raised)


def test_reconfig_apply_and_assert():
    print("prod-reconfig: idempotent apply + assertions")
    fake = FakeOdoo({
        "ir.config_parameter": {1: {"key": "web.base.url", "value": "https://qa.gatheringatthegrove.com"}},
        "ir.cron": {1: {"active": False}, 2: {"active": True}},
        "payment.provider": {1: {"code": "stripe", "state": "test"}},
    })
    spec = {
        "config_parameters": {"web.base.url": "https://prod.example", "web.base.url.freeze": "True"},
        "record_writes": [
            {"label": "crons", "model": "ir.cron", "domain": [["active", "=", False]], "values": {"active": True}},
            {"label": "stripe", "model": "payment.provider", "domain": [["code", "=", "stripe"]],
             "values": {"state": "enabled"}},
        ],
        "assertions": [
            {"label": "base url", "model": "ir.config_parameter", "domain": [["key", "=", "web.base.url"]],
             "field": "value", "expect": "https://prod.example"},
            {"label": "no disabled cron", "model": "ir.cron", "domain": [["active", "=", False]], "count_expect": 0},
            {"label": "stripe enabled", "model": "payment.provider",
             "domain": [["code", "=", "stripe"], ["state", "=", "enabled"]], "min_count": 1},
        ],
    }
    reconfig.apply_config_parameters(fake, spec["config_parameters"])
    reconfig.apply_record_writes(fake, spec["record_writes"])
    _, fails = reconfig.run_assertions(fake, spec["assertions"])
    check("all assertions pass after apply", fails == 0, f"{fails} failed")
    check("base url rewritten", reconfig.get_config_param(fake, "web.base.url") == "https://prod.example")
    check("freeze param created", reconfig.get_config_param(fake, "web.base.url.freeze") == "True")

    # idempotency: a second full apply changes nothing and still passes
    reconfig.apply_config_parameters(fake, spec["config_parameters"])
    reconfig.apply_record_writes(fake, spec["record_writes"])
    _, fails2 = reconfig.run_assertions(fake, spec["assertions"])
    check("second apply still passes (idempotent)", fails2 == 0)

    # a wrong expectation must FAIL (assert, don't assume)
    bad = [{"label": "wrong", "model": "ir.config_parameter", "domain": [["key", "=", "web.base.url"]],
            "field": "value", "expect": "https://WRONG"}]
    _, badf = reconfig.run_assertions(fake, bad)
    check("wrong expectation fails", badf == 1)


# ── promotion-integrity-gates ─────────────────────────────────────────────────

def test_gates_sequence():
    print("integrity-gates: sequence continuity")
    check("trailing S00042 → 42", gates.trailing_number("S00042") == 42)
    check("trailing INV/2026/0007 → 7", gates.trailing_number("INV/2026/0007") == 7)
    check("no digits → None", gates.trailing_number("DRAFT") is None)
    check("max over names", gates.max_trailing_number(["S1", "S00042", "S9"]) == 42)

    good = FakeOdoo({
        "sale.order": {1: {"name": "S00041"}, 2: {"name": "S00042"}},
        "ir.sequence": {9: {"code": "sale.order", "number_next_actual": 43}},
    })
    r = gates.gate_sequence_continuity(good, "sale.order", "sale.order", "name", [], "orders")
    check("next(43) > highest(42) passes", r["ok"], r["detail"])

    collide = FakeOdoo({
        "sale.order": {1: {"name": "S00042"}},
        "ir.sequence": {9: {"code": "sale.order", "number_next_actual": 42}},
    })
    r2 = gates.gate_sequence_continuity(collide, "sale.order", "sale.order", "name", [], "orders")
    check("next(42) == highest(42) FAILS (would collide)", not r2["ok"], r2["detail"])

    missing = FakeOdoo({"sale.order": {1: {"name": "S00001"}}, "ir.sequence": {}})
    r3 = gates.gate_sequence_continuity(missing, "sale.order", "sale.order", "name", [], "orders")
    check("missing sequence with records FAILS", not r3["ok"])

    # pre-launch: no records AND no sequence → SKIP (not FAIL)
    prelaunck = FakeOdoo({"account.move": {}, "ir.sequence": {}})
    r4 = gates.gate_sequence_continuity(prelaunck, "account.move", "account.move.out_invoice", "name",
                                        [["move_type", "=", "out_invoice"], ["state", "=", "posted"]], "invoices")
    check("no records + no sequence = SKIP (pre-launch)", r4.get("skipped") and r4["ok"])


def test_gates_wv_tax():
    print("integrity-gates: WV-nexus tax spot-check")
    # WV order with tax > 0 (ok), non-WV order with 0 tax (ok)
    ok_fake = FakeOdoo({
        "sale.order": {
            1: {"name": "S1", "state": "sale", "amount_tax": 3.50, "partner_shipping_id": [11, "WV cust"]},
            2: {"name": "S2", "state": "sale", "amount_tax": 0.0, "partner_shipping_id": [12, "OH cust"]},
        },
        "res.partner": {11: {"state_id": [100, "WV"]}, 12: {"state_id": [101, "OH"]}},
        "res.country.state": {100: {"code": "WV"}, 101: {"code": "OH"}},
    })
    r = gates.gate_wv_tax_spotcheck(ok_fake)
    check("correct WV/non-WV taxation passes", r["ok"], r["detail"])

    # WV order MISSING tax → violation
    bad_wv = FakeOdoo({
        "sale.order": {1: {"name": "S1", "state": "sale", "amount_tax": 0.0, "partner_shipping_id": [11, "WV"]}},
        "res.partner": {11: {"state_id": [100, "WV"]}},
        "res.country.state": {100: {"code": "WV"}},
    })
    r2 = gates.gate_wv_tax_spotcheck(bad_wv)
    check("WV order with 0 tax FAILS", not r2["ok"], r2["detail"])

    # non-WV order CHARGED tax → violation
    bad_oh = FakeOdoo({
        "sale.order": {1: {"name": "S1", "state": "sale", "amount_tax": 5.0, "partner_shipping_id": [12, "OH"]}},
        "res.partner": {12: {"state_id": [101, "OH"]}},
        "res.country.state": {101: {"code": "OH"}},
    })
    r3 = gates.gate_wv_tax_spotcheck(bad_oh)
    check("non-WV order charged tax FAILS", not r3["ok"], r3["detail"])


def test_gates_completeness():
    print("integrity-gates: completeness vs baseline")
    counts = {"sale_orders": 100, "confirmed_orders": 80, "partners": 50, "posted_invoices": 40}
    check("equal counts pass", gates.gate_completeness(counts, dict(counts))["ok"])
    lost = dict(counts); lost["sale_orders"] = 99
    check("lost row FAILS", not gates.gate_completeness(lost, counts)["ok"])
    check("no baseline is SKIP not FAIL", gates.gate_completeness(counts, None).get("skipped"))


# ── qa-reseed-guard ───────────────────────────────────────────────────────────

def test_reseed_guard():
    print("qa-reseed-guard: block/allow/set/clear")
    empty = FakeOdoo({"ir.config_parameter": {}})
    check("no marker → BLOCK (read_marker None)", guard.read_marker(empty) is None)

    guard.set_marker(empty, {"verified_at": "2026-08-15T00:00:00Z", "dump_sha256": "abc",
                             "prod_confirmed_by": "rick", "note": ""})
    check("after set → marker present (ALLOW)", guard.read_marker(empty) is not None)

    # set is idempotent-ish: setting again just overwrites the single param row
    guard.set_marker(empty, {"verified_at": "2026-08-16T00:00:00Z", "dump_sha256": "def",
                             "prod_confirmed_by": "rick", "note": "re"})
    rows = [r for r in empty.records["ir.config_parameter"].values()
            if r["key"] == guard.MARKER_KEY]
    check("marker stays a single row on re-set", len(rows) == 1)

    existed = guard.clear_marker(empty)
    check("clear removes it", existed and guard.read_marker(empty) is None)


def main() -> int:
    for fn in (test_reconfig_env_refs, test_reconfig_apply_and_assert,
               test_gates_sequence, test_gates_wv_tax, test_gates_completeness,
               test_reseed_guard):
        fn()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
