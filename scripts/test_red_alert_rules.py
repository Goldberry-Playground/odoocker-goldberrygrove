#!/usr/bin/env python3
"""Contract tests between Beyla's RED histograms and the Odoo RED alert rules.

No OpenObserve, no network. Guards the 2026-11-03 stuck latency warning:
Beyla 1.9.4 exports CUMULATIVE histograms (prod OpenObserve:
aggregation_temporality = AGGREGATION_TEMPORALITY_CUMULATIVE on every
series), so a row rule on http_server_request_duration_max matched the slowest
request since Beyla started (/products pinned at 1.67s for hours while the
windowed mean was 0.63s), and a row rule on _count matched any 5xx ever.

  no-cumulative-rows  no rule reads the _max stream or puts a bare row
                      condition on a cumulative histogram stream.
  windowed-delta      every RED rule is SQL that counts requests inside the
                      window as per-series max(value) - min(value), with
                      start_time in the series key so a counter reset is not a
                      spike, and a minimum request count.
  real-bucket-bound   p95 rules compare against a bucket Beyla actually
                      emits; a non-bound le matches no rows and never fires.

    python3 scripts/test_red_alert_rules.py
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ALERTS = Path(__file__).resolve().parent.parent / "openobserve" / "alerts.json"
RED_PREFIX = "http_server_request_duration"
# Beyla's default http_server_request_duration buckets (prod, 11-03).
BUCKET_BOUNDS = {"0", "0.005", "0.01", "0.025", "0.05", "0.075", "0.1", "0.25",
                 "0.5", "0.75", "1", "2.5", "5", "7.5", "10", "inf"}


def _red_rules() -> list[dict]:
    alerts = json.loads(ALERTS.read_text())["alerts"]
    return [a for a in alerts
            if isinstance(a, dict) and a.get("stream_name", "").startswith(RED_PREFIX)]


class RedRules(unittest.TestCase):
    def setUp(self):
        self.rules = _red_rules()
        self.assertTrue(self.rules, "no RED rules found")

    def test_no_rule_reads_cumulative_rows(self):
        for rule in self.rules:
            with self.subTest(rule=rule["name"]):
                self.assertNotEqual(rule["stream_name"], f"{RED_PREFIX}_max")
                self.assertEqual(rule["query_condition"]["type"], "sql")

    def test_sql_counts_requests_inside_the_window(self):
        for rule in self.rules:
            with self.subTest(rule=rule["name"]):
                sql = rule["query_condition"]["sql"]
                self.assertIn(f"FROM {rule['stream_name']} ", sql, "SQL must read the declared stream")
                self.assertIn("max(value) - min(value)", sql)
                self.assertRegex(sql, r"GROUP BY [^)]*\bstart_time\b", "start_time absorbs counter resets")
                self.assertRegex(sql, r"total >= \d+", "needs a minimum request count")
                self.assertIn("service_name = 'odoo'", sql)

    def test_p95_rules_use_real_bucket_bounds(self):
        for rule in self.rules:
            if rule["stream_name"] != f"{RED_PREFIX}_bucket":
                continue
            with self.subTest(rule=rule["name"]):
                les = set(re.findall(r"le = '([^']+)'", rule["query_condition"]["sql"]))
                self.assertIn("inf", les)
                self.assertTrue(les <= BUCKET_BOUNDS, f"not a Beyla bucket bound: {les - BUCKET_BOUNDS}")

    def test_window_rules_fire_on_one_row(self):
        for rule in self.rules:
            with self.subTest(rule=rule["name"]):
                trig = rule["trigger_condition"]
                self.assertEqual((trig["operator"], trig["threshold"]), (">=", 1))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
