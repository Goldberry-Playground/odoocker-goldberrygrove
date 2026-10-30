#!/usr/bin/env python3
"""Contract tests between the synthetic runner and its OpenObserve alert rules.

Guards the 10-30 false-critical pages: a catalog assertion on a field the API
never serializes, and alert rules that matched any failing journey because the
journey filter lived only in the description. No Hurl, OpenObserve, or network:
    python3 synthetic/test_alert_contract.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import run

REPO = Path(__file__).resolve().parent.parent
ALERTS = REPO / "openobserve" / "alerts.json"

# Journey label values run.py emits. ghost-content and checkout-canary are
# appended inline in main(), not via the journey tables.
EMITTED_JOURNEYS = (
    {name for name, _ in run.SHARED_JOURNEYS}
    | {name for name, _ in run.TENANT_JOURNEYS}
    | {"ghost-content", "checkout-canary"}
)
# The success stream carries the gauge in `value` plus the OTLP attributes.
SUCCESS_COLUMNS = {"value", "journey", "tenant", "tier", "env"}


def _success_rules() -> list[dict]:
    alerts = json.loads(ALERTS.read_text())["alerts"]
    return [
        a for a in alerts
        if isinstance(a, dict) and a.get("stream_name") == "synthetic_journey_success"
    ]


def _conditions(rule: dict) -> tuple[str, list[dict]]:
    conds = rule["query_condition"]["conditions"]
    (op, items), = conds.items()
    return op, items


def test_every_success_rule_is_scoped_to_one_emitted_journey() -> None:
    rules = _success_rules()
    assert rules, "no synthetic_journey_success rules found"
    for rule in rules:
        op, items = _conditions(rule)
        journeys = [c["value"] for c in items if c["column"] == "journey" and c["operator"] == "="]
        # OR would let any failing journey match; the filter only scopes under AND.
        assert op == "and", f"{rule['name']}: conditions must be AND-ed, got {op}"
        assert len(journeys) == 1, f"{rule['name']}: needs exactly one journey= filter, got {journeys}"
        assert journeys[0] in EMITTED_JOURNEYS, f"{rule['name']}: journey {journeys[0]!r} is never emitted"


def test_success_rules_only_query_columns_the_stream_has() -> None:
    for rule in _success_rules():
        _, items = _conditions(rule)
        cols = {c["column"] for c in items}
        assert cols <= SUCCESS_COLUMNS, f"{rule['name']}: unknown columns {cols - SUCCESS_COLUMNS}"


def test_catalog_asserts_the_serialized_price_field() -> None:
    hurl = (Path(__file__).resolve().parent / "journeys" / "catalog.hurl").read_text()
    # grove_headless serializes variant prices as `price`; `lst_price` never appears.
    assert 'jsonpath "$.variants[0].price" exists' in hurl
    assert "lst_price" not in hurl


if __name__ == "__main__":
    test_every_success_rule_is_scoped_to_one_emitted_journey()
    test_success_rules_only_query_columns_the_stream_has()
    test_catalog_asserts_the_serialized_price_field()
    print("ok: synthetic alert contract", file=sys.stderr)
