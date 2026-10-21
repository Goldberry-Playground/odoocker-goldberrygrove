# qa-settlement-check.py — payload for scripts/qa-settlement-check.sh.
#
# Runs INSIDE `odoo shell` on the L3 QA droplet (fed on stdin, so `env` is the
# shell's global). Never run it against prod: the wrapper only targets QA, and
# the key guard below refuses anything but a Stripe TEST key.
#
# What it proves: the ship-time balance capture (GOL-2053) works end to end
# against REAL Stripe test mode. The e2e gate's checkout-deposit-happy-path
# spec pays a $10 deposit through Stripe's hosted page and saves the card;
# nothing in CI then ships that order, so the off-session balance charge —
# the bulk of every pre-order's money — was only ever unit-tested with
# Stripe mocked. This takes one such deposit_paid order, simulates the label
# purchase (watermark + actual shipping cost, no real label bought), runs the
# same _operator_mark_shipped the Discord "Mark Shipped" button runs, and
# asserts the balance was captured.
#
# Inputs (env, set by the wrapper):
#   QA_SETTLE_ORDER      sale.order name (e.g. S00412); empty = newest eligible
#   QA_SETTLE_SHIP_COST  simulated label cost in USD (default 9.00)
#   QA_SETTLE_APPLY      "1" to actually ship + charge; anything else = dry run
#
# Output: human-readable lines, then exactly one "RESULT: PASS|FAIL|DRYRUN ..."
# line the wrapper greps for its exit code (odoo shell swallows SystemExit).
import os
from datetime import timedelta

from odoo.addons.grove_headless.controllers import main as grove_main

from odoo import fields

ACTOR = "qa-settlement-check"
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
MAX_AGE = timedelta(hours=72)


def _result(status, msg):
    print(f"RESULT: {status} {msg}")


def _run():
    ref = (os.environ.get("QA_SETTLE_ORDER") or "").strip()
    apply_ = os.environ.get("QA_SETTLE_APPLY") == "1"
    ship_cost = float(os.environ.get("QA_SETTLE_SHIP_COST") or "9.00")

    SaleOrder = env["sale.order"].sudo()  # noqa: F821 — `env` is the odoo shell global
    # The card is cached on the order lazily: checkout saves only the customer
    # and the deposit PaymentIntent, and _resolve_saved_card reads the
    # payment_method back from that intent at first settlement. So an order is
    # chargeable with EITHER a cached pm_ OR a deposit pi_ to recover it from.
    domain = [
        ("grove_fulfillment", "=", "ship"),
        ("grove_checkout_status", "=", "deposit_paid"),
        ("grove_stripe_customer", "=like", "cus_%"),
        "|",
        ("grove_stripe_payment_method", "=like", "pm_%"),
        ("grove_stripe_payment_intent", "=like", "pi_%"),
    ]
    if ref:
        domain.append(("name", "=", ref))
    else:
        domain.append(("create_date", ">=", fields.Datetime.now() - MAX_AGE))
    order = SaleOrder.search(domain, order="create_date desc", limit=1)
    if not order:
        scope = (
            ref or f"any order from the last {int(MAX_AGE.total_seconds() // 3600)}h"
        )
        return _result(
            "FAIL",
            f"no ship-method deposit_paid order with a Stripe customer + card or deposit intent ({scope}). "
            "Run the e2e gate (checkout-deposit-happy-path) first, or pass ORDER=<name>.",
        )

    # Money guard: resolve the EXACT key settle_order_at_ship will charge with
    # and refuse anything that is not a Stripe test key. Print the prefix only.
    tenant = order.website_id.grove_tenant_slug() if order.website_id else None
    key = grove_main._tenant_secret_key(tenant) or ""
    if not key.startswith(TEST_KEY_PREFIXES):
        return _result(
            "FAIL",
            f"refusing: tenant {tenant!r} resolves a non-test Stripe key (prefix {key[:8]!r}). "
            "This check only runs against Stripe TEST mode.",
        )

    deposit = order.grove_amount_charged_today or 0.0
    print(
        f">> order {order.name} (id {order.id}, tenant {tenant}, created {order.create_date})"
    )
    print(f">> Stripe key mode: {key[:8]}… (test)")
    print(
        f">> deposit already charged: {deposit:.2f}; Odoo total before label: {order.amount_total:.2f}"
    )
    print(f">> simulated label cost: {ship_cost:.2f}")

    if not apply_:
        env.cr.rollback()  # noqa: F821
        return _result(
            "DRYRUN",
            f"{order.name} is eligible; re-run with APPLY=1 to ship it and charge the balance",
        )

    # Simulate the label milestone the Pirate Ship / Shippo flow would record,
    # without buying a real label, then mark shipped exactly as the Discord
    # button does. Settlement and the shipment email both fire off that move.
    order.write(
        {
            "grove_fulfillment_state": "label_purchased",
            "grove_actual_shipping_cost": ship_cost,
        }
    )
    outcome = grove_main._operator_mark_shipped(env, order, actor=ACTOR, source=ACTOR)  # noqa: F821
    env.cr.commit()  # noqa: F821 — odoo shell does not autocommit; the charge is real, so keep the record of it
    order.invalidate_recordset()

    pi = order.grove_settlement_payment_intent or ""
    print(
        f">> mark-shipped: newly_shipped={outcome['newly_shipped']} settlement={outcome['settlement']}"
    )
    print(
        f">> checkout status now: {order.grove_checkout_status}; settlement PaymentIntent: {pi or '(none)'}"
    )
    print(
        f">> Odoo total after label: {order.amount_total:.2f}; balance captured ≈ {order.amount_total - deposit:.2f}"
    )

    if (
        outcome["settlement"] == "settled"
        and order.grove_checkout_status == "settled"
        and pi.startswith("pi_")
    ):
        return _result(
            "PASS",
            f"{order.name} balance captured off-session on Stripe test mode ({pi})",
        )
    return _result(
        "FAIL",
        f"{order.name} settlement={outcome['settlement']!r}, status={order.grove_checkout_status!r}, "
        f"pi={pi or None!r}. Read the order chatter for the gateway reason.",
    )


try:
    _run()
except Exception as exc:  # noqa: BLE001 — surface any crash as a FAIL line, never a silent shell exit
    env.cr.rollback()  # noqa: F821
    _result("FAIL", f"crashed: {type(exc).__name__}: {exc}")
