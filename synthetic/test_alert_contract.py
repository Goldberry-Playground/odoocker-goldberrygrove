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

import probes
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


# --- synthetic_uptime rules (11-11 page storm) ---------------------------------
# One slow ggg-root probe (15/16 up) fired frontend, odoo, ghost AND postgres
# pages in the same minute: every uptime rule was a bare `success = 0`, so any
# failing target matched all of them. Each rule must name the targets it means.
UPTIME_COLUMNS = {"success", "target", "tenant", "tier", "service", "route", "env"}
UPTIME_SCOPE_COLUMNS = {"target", "tier", "service", "route"}
# Rule-name prefix -> the identity every matched target must share.
UPTIME_RULE_OWNERS = {
    "frontend-": ("tier", "frontend"),
    "odoo-": ("service", "odoo"),
    "ghost-": ("service", "ghost"),
    "postgres-": ("service", "postgres"),
}


def _uptime_rules() -> list[dict]:
    alerts = json.loads(ALERTS.read_text())["alerts"]
    return [
        a for a in alerts
        if isinstance(a, dict) and a.get("stream_name") == "synthetic_uptime"
    ]


def _uptime_targets() -> list[dict]:
    return [probes._identity(m) for m in probes.load_monitors() if m["type"] in ("http", "tcp")]


def _matches(items: list[dict], ident: dict) -> bool:
    return all(ident.get(c["column"]) == c["value"] for c in items if c["column"] != "success")


def test_every_uptime_rule_is_scoped_and_and_ed() -> None:
    rules = _uptime_rules()
    assert rules, "no synthetic_uptime rules found"
    for rule in rules:
        op, items = _conditions(rule)
        assert op == "and", f"{rule['name']}: conditions must be AND-ed, got {op}"
        cols = {c["column"] for c in items}
        assert cols <= UPTIME_COLUMNS, f"{rule['name']}: unknown columns {cols - UPTIME_COLUMNS}"
        assert any(c["column"] == "success" and c["value"] == "0" for c in items), (
            f"{rule['name']}: must still require success = 0"
        )
        assert cols & UPTIME_SCOPE_COLUMNS, f"{rule['name']}: needs a target/tier/service/route filter"


def test_uptime_rules_only_match_their_own_targets() -> None:
    targets = _uptime_targets()
    for rule in _uptime_rules():
        prefix = next((p for p in UPTIME_RULE_OWNERS if rule["name"].startswith(p)), None)
        assert prefix, f"{rule['name']}: no owner mapping; add it to UPTIME_RULE_OWNERS"
        col, want = UPTIME_RULE_OWNERS[prefix]
        _, items = _conditions(rule)
        matched = [t for t in targets if _matches(items, t)]
        assert matched, f"{rule['name']}: matches no monitored target"
        strays = [t["target"] for t in matched if t.get(col) != want]
        assert not strays, f"{rule['name']}: would page on unrelated targets {strays}"


def test_one_failing_storefront_pages_only_frontend_rules() -> None:
    ggg_root = next(t for t in _uptime_targets() if t["target"] == "ggg-root")
    fired = sorted(r["name"] for r in _uptime_rules() if _matches(_conditions(r)[1], ggg_root))
    assert fired == ["frontend-down-critical", "frontend-down-warning"], fired


if __name__ == "__main__":
    test_every_success_rule_is_scoped_to_one_emitted_journey()
    test_success_rules_only_query_columns_the_stream_has()
    test_catalog_asserts_the_serialized_price_field()
    test_every_uptime_rule_is_scoped_and_and_ed()
    test_uptime_rules_only_match_their_own_targets()
    test_one_failing_storefront_pages_only_frontend_rules()
    print("ok: synthetic alert contract", file=sys.stderr)
