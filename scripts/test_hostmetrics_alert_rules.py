#!/usr/bin/env python3
"""Contract tests between the collector's hostmetrics and their alert rules.

No OpenObserve, no network. Guards the 2026-11-03 false critical page: the
first real hostmetrics on prod (after odoocker-goldberrygrove#980) carried
system_cpu_utilization per cpu AND per state, so `value > 0.90` matched the
idle series (~0.93 on a 7%-busy box) and droplet-cpu-critical paged
permanently. Checked against prod OpenObserve that day:

  cpu       states idle/user/system/steal/... ; idle avg 0.929, max 0.984
  memory    states used/free/cached/buffered/slab_*; cached alone ~0.56

  cpu-reads-idle     every CPU rule filters state='idle' and fires BELOW its
                     ratio (busy = 1 - idle); a bare upper bound on value
                     would match idle again.
  cpu-is-sustained   CPU rules need many rows per window, not one sample: a
                     single per-core 30s sample under the line is a burst,
                     not the ">x% for 10m" the description promises.
  memory-reads-used  every memory rule filters state='used'.

    python3 scripts/test_hostmetrics_alert_rules.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ALERTS = Path(__file__).resolve().parent.parent / "openobserve" / "alerts.json"
# Rows one 2-vCPU host emits per 10m window at the 30s collection interval.
CPU_ROWS_PER_WINDOW = 2 * (10 * 60 // 30)


def _rules(stream: str) -> list[dict]:
    alerts = json.loads(ALERTS.read_text())["alerts"]
    return [a for a in alerts if isinstance(a, dict) and a.get("stream_name") == stream]


def _and_conditions(rule: dict) -> list[dict]:
    conds = rule["query_condition"]["conditions"]
    assert list(conds) == ["and"], f"{rule['name']}: conditions must be AND-ed"
    return conds["and"]


def _state(items: list[dict]) -> list[str]:
    return [c["value"] for c in items if c["column"] == "state" and c["operator"] == "="]


class CpuRules(unittest.TestCase):
    def setUp(self):
        self.rules = _rules("system_cpu_utilization")
        self.assertTrue(self.rules, "no system_cpu_utilization rules")

    def test_cpu_rules_read_idle_below_threshold(self):
        for rule in self.rules:
            with self.subTest(rule=rule["name"]):
                items = _and_conditions(rule)
                self.assertEqual(_state(items), ["idle"])
                ops = {c["operator"] for c in items if c["column"] == "value"}
                self.assertEqual(ops, {"<"}, "busy = 1 - idle, so fire below the idle ratio")

    def test_cpu_rules_are_sustained_not_single_sample(self):
        for rule in self.rules:
            with self.subTest(rule=rule["name"]):
                trig = rule["trigger_condition"]
                self.assertEqual(trig["period"], 10)
                self.assertGreaterEqual(trig["threshold"], CPU_ROWS_PER_WINDOW // 2)
                self.assertLessEqual(trig["threshold"], CPU_ROWS_PER_WINDOW)


class MemoryRules(unittest.TestCase):
    def test_memory_rules_read_used(self):
        rules = _rules("system_memory_utilization")
        self.assertTrue(rules, "no system_memory_utilization rules")
        for rule in rules:
            with self.subTest(rule=rule["name"]):
                self.assertEqual(_state(_and_conditions(rule)), ["used"])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
