#!/usr/bin/env python3
"""Unit tests for scripts/stripe-tax-gate.py (GOL-2568 Gates 3/4).

Stdlib unittest, no network, no Odoo, no Stripe — the assertion logic is pure
and that is deliberate: the part of this tool that decides whether a money-path
flag may flip has to be testable without a live QA, because the window in which
a live QA exists is exactly when there is no time to debug the gate.

The invariant under test throughout: **an inconclusive run must never read as
permission to flip.**
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest

# The worker is hyphenated (it is an operator entrypoint, not an importable
# module), so load it by path. It must be registered in sys.modules BEFORE
# exec_module: @dataclass resolves annotations through sys.modules[cls.__module__]
# and raises on an unregistered module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("stg", os.path.join(_HERE, "stripe-tax-gate.py"))
stg = importlib.util.module_from_spec(_spec)
sys.modules["stg"] = stg
_spec.loader.exec_module(stg)


def _case(name, status=stg.STATUS_PASS):
    return stg.CaseResult(name=name, gate=stg.CASES[name]["gate"], status=status,
                          expect_flag=stg.CASES[name]["expect_flag"])


def _session(total_cents, tax_cents=0, auto=False):
    return {
        "amount_total": total_cents,
        "total_details": {"amount_tax": tax_cents},
        "automatic_tax": {"enabled": auto, "status": "complete" if auto else None},
    }


class TestFlagObservation(unittest.TestCase):
    def test_automatic_tax_enabled_means_flag_on(self):
        self.assertEqual(stg.observed_flag_state(_session(10000, 600, auto=True)), "on")

    def test_absent_or_disabled_automatic_tax_means_off(self):
        self.assertEqual(stg.observed_flag_state(_session(10000)), "off")
        self.assertEqual(stg.observed_flag_state({}), "off")


class TestTaxArithmetic(unittest.TestCase):
    def test_base_is_total_minus_the_tax_stripe_added(self):
        self.assertEqual(stg.taxable_base_cents(_session(10600, 600, auto=True), 600), 10000)

    def test_wv_state_rate_is_six_percent(self):
        self.assertEqual(stg.expected_tax_cents(10000, 6.0), 600)

    def test_observed_rate_is_reported_not_divided_by_zero(self):
        self.assertEqual(stg.observed_rate_pct(0, 0), 0.0)
        self.assertEqual(stg.observed_rate_pct(10000, 700), 7.0)


class TestWvTaxLineDetection(unittest.TestCase):
    def test_matches_both_rendered_names(self):
        # _wv_tax_line_name renders either form depending on whether a pct resolved.
        self.assertTrue(stg.has_explicit_wv_tax_line([{"description": "Sales tax (WV)"}]))
        self.assertTrue(stg.has_explicit_wv_tax_line([{"description": "WV Sales Tax (6%)"}]))

    def test_reads_the_expanded_price_product_name_too(self):
        self.assertTrue(stg.has_explicit_wv_tax_line(
            [{"price": {"product": {"name": "WV Sales Tax (6%)"}}}]))

    def test_does_not_match_ordinary_goods(self):
        self.assertFalse(stg.has_explicit_wv_tax_line(
            [{"description": "Pawpaw, bareroot"}, {"description": "Shipping"}]))


class TestGate4aWvFull(unittest.TestCase):
    def test_six_percent_on_a_wv_destination_passes(self):
        res = _case("wv_full")
        res.observed_flag = "on"
        out = stg.evaluate_wv_full(res, _session(10600, 600, auto=True), [], 6.0)
        self.assertEqual(out.status, stg.STATUS_PASS, out.reasons)

    def test_zero_tax_is_named_as_the_missing_registration(self):
        res = _case("wv_full")
        res.observed_flag = "on"
        out = stg.evaluate_wv_full(res, _session(10000, 0, auto=True), [], 6.0)
        self.assertEqual(out.status, stg.STATUS_FAIL)
        self.assertEqual(out.reasons[0]["code"], "NO_TAX")

    def test_seven_percent_is_rate_mismatch_not_no_tax(self):
        # GOL-2449: QA fixtures bind the 6% STATE rate, not the 7% state+municipal
        # group. Confusing the two failures sends the operator to the wrong fix.
        res = _case("wv_full")
        res.observed_flag = "on"
        out = stg.evaluate_wv_full(res, _session(10700, 700, auto=True), [], 6.0)
        self.assertEqual(out.reasons[0]["code"], "RATE_MISMATCH")

    def test_one_cent_of_per_line_rounding_is_tolerated(self):
        res = _case("wv_full")
        res.observed_flag = "on"
        out = stg.evaluate_wv_full(res, _session(10601, 601, auto=True), [], 6.0)
        self.assertEqual(out.status, stg.STATUS_PASS, out.reasons)

    def test_both_tax_sources_present_is_double_tax(self):
        res = _case("wv_full")
        res.observed_flag = "on"
        out = stg.evaluate_wv_full(res, _session(10600, 600, auto=True),
                                   [{"description": "Sales tax (WV)"}], 6.0)
        self.assertEqual(out.reasons[0]["code"], "DOUBLE_TAX")

    def test_flag_still_off_fails_with_the_recreate_hint(self):
        res = _case("wv_full")
        res.observed_flag = "off"
        out = stg.evaluate_wv_full(res, _session(10000, 0), [], 6.0)
        self.assertEqual(out.reasons[0]["code"], "FLAG_NOT_ON")


class TestGate4bNonWv(unittest.TestCase):
    def test_zero_tax_outside_nexus_passes(self):
        res = _case("nonwv_full")
        res.observed_flag = "on"
        self.assertEqual(stg.evaluate_nonwv_full(res, _session(10000, 0, auto=True), []).status,
                         stg.STATUS_PASS)

    def test_any_tax_outside_nexus_fails(self):
        res = _case("nonwv_full")
        res.observed_flag = "on"
        out = stg.evaluate_nonwv_full(res, _session(10600, 600, auto=True), [])
        self.assertEqual(out.reasons[0]["code"], "UNEXPECTED_TAX")


class TestGate4cDeposit(unittest.TestCase):
    """The case the prose gate omits — and the one that carries orders after Oct 15."""

    def test_flat_ten_dollar_deposit_with_tax_stood_down_passes(self):
        res = _case("deposit")
        res.observed_flag = "off"
        out = stg.evaluate_deposit(res, _session(1000, 0),
                                   {"has_preorder": True, "amount_due_today": 10.0,
                                    "amount_total": 212.0})
        self.assertEqual(out.status, stg.STATUS_PASS, out.reasons)

    def test_automatic_tax_on_a_deposit_is_a_double_tax_failure(self):
        res = _case("deposit")
        res.observed_flag = "on"
        out = stg.evaluate_deposit(res, _session(1060, 60, auto=True),
                                   {"has_preorder": True, "amount_due_today": 10.0})
        self.assertEqual(out.reasons[0]["code"], "TAX_ON_DEPOSIT")

    def test_a_cart_that_never_triggered_a_deposit_proves_nothing(self):
        res = _case("deposit")
        res.observed_flag = "off"
        out = stg.evaluate_deposit(res, _session(21200, 0), {"has_preorder": False})
        self.assertEqual(out.reasons[0]["code"], "NOT_A_DEPOSIT")

    def test_deposit_must_be_flat_ten_per_order_not_per_tree(self):
        # GOL-2233: ONE flat $10 for the WHOLE order, 100 trees included.
        res = _case("deposit")
        res.observed_flag = "off"
        out = stg.evaluate_deposit(res, _session(5000, 0),
                                   {"has_preorder": True, "amount_due_today": 50.0})
        self.assertEqual(out.reasons[0]["code"], "DEPOSIT_AMOUNT")


class TestGate3Rollback(unittest.TestCase):
    def test_odoo_line_back_and_no_stripe_tax_passes(self):
        res = _case("rollback_wv")
        res.observed_flag = "off"
        out = stg.evaluate_rollback_wv(res, _session(10600, 0),
                                       [{"description": "WV Sales Tax (6%)"}])
        self.assertEqual(out.status, stg.STATUS_PASS, out.reasons)

    def test_missing_odoo_tax_line_blocks_the_flip(self):
        # Rollback that under-collects WV sales tax is the failure that MUST be red.
        res = _case("rollback_wv")
        res.observed_flag = "off"
        out = stg.evaluate_rollback_wv(res, _session(10000, 0), [{"description": "Pawpaw"}])
        self.assertEqual(out.reasons[0]["code"], "NO_ODOO_TAX_LINE")

    def test_flag_still_on_is_not_a_rollback(self):
        res = _case("rollback_wv")
        res.observed_flag = "on"
        out = stg.evaluate_rollback_wv(res, _session(10600, 600, auto=True), [])
        self.assertEqual(out.reasons[0]["code"], "FLAG_NOT_OFF")


class TestVerdictAggregation(unittest.TestCase):
    def _all_pass(self):
        return [_case(n) for n in ("wv_full", "nonwv_full", "deposit", "rollback_wv")]

    def test_all_four_cases_passing_clears_the_flip(self):
        v = stg.aggregate(self._all_pass())
        self.assertTrue(v["flip_allowed"])
        self.assertEqual(v["gate_3_rollback"]["status"], stg.STATUS_PASS)
        self.assertEqual(v["gate_4_amounts"]["status"], stg.STATUS_PASS)

    def test_gate4_alone_does_not_clear_the_flip(self):
        v = stg.aggregate([_case(n) for n in ("wv_full", "nonwv_full", "deposit")])
        self.assertFalse(v["flip_allowed"])
        self.assertEqual(v["gate_3_rollback"]["missing_cases"], ["rollback_wv"])

    def test_a_missing_deposit_case_leaves_gate4_red(self):
        # The headline regression risk: running only the two prose cases and
        # reading the green as a licence to flip.
        v = stg.aggregate([_case("wv_full"), _case("nonwv_full"), _case("rollback_wv")])
        self.assertFalse(v["flip_allowed"])
        self.assertEqual(v["gate_4_amounts"]["missing_cases"], ["deposit"])

    def test_no_results_at_all_is_not_permission(self):
        v = stg.aggregate([])
        self.assertFalse(v["flip_allowed"])
        self.assertIn("Train #3", v["ruling"])

    def test_one_failed_case_sinks_its_gate(self):
        results = self._all_pass()
        results[1].status = stg.STATUS_FAIL
        v = stg.aggregate(results)
        self.assertFalse(v["flip_allowed"])
        self.assertEqual(v["gate_4_amounts"]["failed_cases"], ["nonwv_full"])

    def test_ruling_text_states_the_inert_fallback(self):
        self.assertIn("INERT", stg.aggregate([])["ruling"])


class TestNoSelfSkip(unittest.TestCase):
    """docs/stripe-tax-cutover.md: "do not gate the assertion on key presence"."""

    def test_missing_credentials_fail_rather_than_skip(self):
        args = argparse.Namespace(
            odoo_url="", api_key="", stripe_key="", tenant="nursery",
            variant_instock=None, variant_bareroot=None, wv_rate=6.0, quantity=1)
        problems = stg.Runner(args).preconditions()
        self.assertTrue(problems)
        self.assertTrue(any("QA_ODOO_URL" in p for p in problems))

    def test_a_live_stripe_key_is_refused(self):
        args = argparse.Namespace(
            odoo_url="https://odoo.qa.example.invalid", api_key="k",
            stripe_key="sk_live_abc", tenant="nursery",
            variant_instock="1", variant_bareroot="2", wv_rate=6.0, quantity=1)
        problems = stg.Runner(args).preconditions()
        self.assertTrue(any("sk_test" in p for p in problems))

    def test_a_non_qa_host_is_refused_because_this_creates_orders(self):
        args = argparse.Namespace(
            odoo_url="https://odoo.gatheringatthegrove.com", api_key="k",
            stripe_key="sk_test_abc", tenant="nursery",
            variant_instock="1", variant_bareroot="2", wv_rate=6.0, quantity=1)
        problems = stg.Runner(args).preconditions()
        self.assertTrue(any("does not say 'qa'" in p for p in problems))

    def test_dry_run_reports_unmet_preconditions_and_exits_nonzero(self):
        rc = stg.main(["--dry-run", "--gate", "4", "--odoo-url", "", "--api-key", "",
                       "--stripe-key", ""])
        self.assertEqual(rc, 2)

    def test_unmet_preconditions_make_every_requested_case_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "v.json")
            rc = stg.main(["--gate", "4", "--odoo-url", "", "--api-key", "",
                           "--stripe-key", "", "--json-out", out])
            self.assertEqual(rc, 1)
            with open(out) as fh:
                v = json.load(fh)
            self.assertFalse(v["flip_allowed"])
            self.assertEqual(sorted(v["gate_4_amounts"]["failed_cases"]),
                             ["deposit", "nonwv_full", "wv_full"])


class TestMergingTwoRuns(unittest.TestCase):
    def test_gate4_then_gate3_merge_into_one_flip_decision(self):
        # Gates 3 and 4 need OPPOSITE flag states, so they cannot run in one pass;
        # --merge is what keeps the two halves one auditable verdict.
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "g4.json")
            with open(first, "w") as fh:
                json.dump(stg.aggregate([_case(n) for n in ("wv_full", "nonwv_full", "deposit")]), fh)
            with open(first) as fh:
                earlier = json.load(fh)["cases"]
            merged = stg.aggregate(
                [stg.CaseResult(**c) for c in earlier] + [_case("rollback_wv")])
            self.assertTrue(merged["flip_allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
