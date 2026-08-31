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
        if method == "create":
            vals = args[0]
            rows = self.records.setdefault(model, {})
            new_id = (max(rows) + 1) if rows else 1
            rows[new_id] = dict(vals)
            return new_id
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


def test_reconfig_create_if_missing():
    print("prod-reconfig: create_if_missing provisions a missing row (GOL-1183)")
    # A bare-bootstrapped prod DB has NO ir.mail_server row (unlike a QA-promotion
    # restore, which carries one). A plain repoint-write matches nothing there —
    # exactly how prod launched with no mail server (GOL-1183). create_if_missing
    # must provision it; a re-apply must then match and NOT duplicate.
    fake = FakeOdoo({"ir.mail_server": {}})
    writes = [{
        "label": "mail server", "model": "ir.mail_server",
        "domain": [["smtp_host", "=", "smtp.mailgun.org"]],
        "values": {"smtp_host": "smtp.mailgun.org", "smtp_port": 587},
        "create_if_missing": True,
        "create_values": {"name": "Mailgun (prod)"},  # required-on-create, omitted by a repoint
    }]

    r1 = reconfig.apply_record_writes(fake, writes)
    check("bare DB apply → created==1", r1[0]["created"] == 1, f"created={r1[0]['created']}")
    check("exactly one ir.mail_server row now present",
          fake.call("ir.mail_server", "search_count", [[]]) == 1)
    row = fake.call("ir.mail_server", "search_read", [[]],
                    {"fields": ["name", "smtp_host", "smtp_port"]})[0]
    check("created row merges create_values over values",
          row["name"] == "Mailgun (prod)" and row["smtp_host"] == "smtp.mailgun.org"
          and row["smtp_port"] == 587, f"row={row}")

    # re-apply: the row now matches the domain → write path, no second create
    r2 = reconfig.apply_record_writes(fake, writes)
    check("re-apply → created==0 (matched existing)", r2[0]["created"] == 0, f"created={r2[0]['created']}")
    check("still exactly one row (idempotent, no duplicate)",
          fake.call("ir.mail_server", "search_count", [[]]) == 1)

    # without create_if_missing a bare-DB write stays a silent no-op — the exact
    # GOL-1183 failure mode, locked in so the flag can't be "simplified" away.
    bare = FakeOdoo({"ir.mail_server": {}})
    no_create = [{
        "label": "mail server", "model": "ir.mail_server",
        "domain": [["smtp_host", "=", "smtp.mailgun.org"]],
        "values": {"smtp_host": "smtp.mailgun.org", "smtp_port": 587},
    }]
    r3 = reconfig.apply_record_writes(bare, no_create)
    check("no create_if_missing → no-op (created==0, matched==0)",
          r3[0]["created"] == 0 and r3[0]["matched"] == 0, r3[0])
    check("no row provisioned without the flag",
          bare.call("ir.mail_server", "search_count", [[]]) == 0)


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


def test_gates_qa_fixture_absence():
    print("integrity-gates: QA-fixture/placeholder absence (launch audit item 3)")
    clean = FakeOdoo({"product.template": {
        1: {"name": "Persimmon (bareroot)", "sale_ok": True},
        2: {"name": "Apple (potted)", "sale_ok": True},
    }})
    check("no fixtures → PASS", gates.gate_qa_fixture_absence(clean)["ok"])

    dirty = FakeOdoo({"product.template": {
        1: {"name": "Persimmon (bareroot)", "sale_ok": True},
        2: {"name": "AAA QA E2E bareroot", "sale_ok": True},
        3: {"name": "Coming Soon — mystery pear", "sale_ok": True},
        4: {"name": "Fig — Price TBD", "sale_ok": True},
    }})
    r = gates.gate_qa_fixture_absence(dirty)
    check("QA E2E + Coming Soon + Price TBD present → FAIL", not r["ok"], r["detail"])
    check("all three fixtures reported", len(r["found"]) == 3, r["found"])
    # case-insensitive: lowercase must still be caught
    lc = FakeOdoo({"product.template": {1: {"name": "aaa qa e2e potted", "sale_ok": True}}})
    check("lowercase fixture still caught (=ilike)", not gates.gate_qa_fixture_absence(lc)["ok"])


def test_gates_branding_present():
    print("integrity-gates: branding binaries present (launch audit item 1)")
    ok_fake = FakeOdoo({
        "res.company": {1: {"name": "Goldberry Grove", "logo": "iVBORw0KGgo="}},
        "website": {1: {"name": "Main", "logo": "iVBORw0KGgo=", "favicon": "AAAB"}},
    })
    check("company+website branding present → PASS", gates.gate_branding_present(ok_fake)["ok"])

    # the exact prod-launch defect: company logo empty (default 'Your Logo')
    empty_logo = FakeOdoo({
        "res.company": {1: {"name": "Goldberry Grove", "logo": False}},
        "website": {1: {"name": "Main", "logo": "iVBOR", "favicon": "AAAB"}},
    })
    r = gates.gate_branding_present(empty_logo)
    check("empty res.company.logo → FAIL", not r["ok"], r["detail"])

    empty_favicon = FakeOdoo({
        "res.company": {1: {"name": "GG", "logo": "x"}},
        "website": {1: {"name": "Main", "logo": "x", "favicon": False}},
    })
    check("empty website favicon → FAIL", not gates.gate_branding_present(empty_favicon)["ok"])

    # no website module installed (empty model) is fine as long as company logo set
    no_site = FakeOdoo({"res.company": {1: {"name": "GG", "logo": "x"}}})
    check("no website rows + company logo set → PASS", gates.gate_branding_present(no_site)["ok"])


def test_gates_price_parity():
    print("integrity-gates: price parity vs source (launch audit item 2)")
    baseline = {"product_prices": [
        {"key": "PERSIMMON", "name": "Persimmon", "list_price": 39.0},
        {"key": "APPLE-POT", "name": "Apple (potted)", "list_price": 37.0},
    ]}
    matched = FakeOdoo({"product.template": {
        1: {"name": "Persimmon", "default_code": "PERSIMMON", "list_price": 39.0, "sale_ok": True},
        2: {"name": "Apple (potted)", "default_code": "APPLE-POT", "list_price": 37.0, "sale_ok": True},
    }})
    check("equal prices → PASS", gates.gate_price_parity(matched, baseline)["ok"])

    # the exact observed drift: prod Persimmon $12 vs QA $39
    drifted = FakeOdoo({"product.template": {
        1: {"name": "Persimmon", "default_code": "PERSIMMON", "list_price": 12.0, "sale_ok": True},
        2: {"name": "Apple (potted)", "default_code": "APPLE-POT", "list_price": 35.0, "sale_ok": True},
    }})
    r = gates.gate_price_parity(drifted, baseline)
    check("price drift $39→$12 → FAIL", not r["ok"], r["detail"])
    check("both drifted rows reported", len(r["drift"]) == 2, r["drift"])

    # a product missing on target is reported but NOT a hard fail (completeness's job)
    partial = FakeOdoo({"product.template": {
        1: {"name": "Persimmon", "default_code": "PERSIMMON", "list_price": 39.0, "sale_ok": True},
    }})
    r2 = gates.gate_price_parity(partial, baseline)
    check("missing-on-target is not a parity FAIL", r2["ok"], r2["detail"])
    check("missing product recorded", r2["missing_on_target"] == ["APPLE-POT"], r2["missing_on_target"])

    check("no baseline sample → SKIP", gates.gate_price_parity(matched, None).get("skipped"))
    check("baseline without product_prices → SKIP",
          gates.gate_price_parity(matched, {"sale_orders": 5}).get("skipped"))


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
               test_reconfig_create_if_missing,
               test_gates_sequence, test_gates_wv_tax, test_gates_completeness,
               test_gates_qa_fixture_absence, test_gates_branding_present,
               test_gates_price_parity,
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
