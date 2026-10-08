#!/usr/bin/env python3
"""Stripe Tax cutover gate runner — GOL-2568 Gates 3 and 4 on a live QA.

Josh's ruling (GOL-2584, 2026-10-02): grove_headless#284 rides Train #2 with
every ``GROVE_STRIPE_TAX_*`` flag OFF ("ships inert"). The flag flips at the
Wed promote **only if Gates 3 and 4 pass on QA**. Those gates were specified in
prose (grove-odoo-modules ``docs/stripe-tax-cutover.md``) and executed by hand,
which on a money path means the flip decision rests on an operator's reading of
a Stripe dashboard. This turns them into one command with a machine-readable
verdict.

WHY THIS IS NOT A PLAYWRIGHT TEST
--------------------------------
The amount Stripe will charge is fixed when the Checkout Session is created:
``automatic_tax`` resolves at creation time because grove_headless attaches a
Stripe Customer carrying the ship-to (``_ensure_stripe_customer``) rather than
using ``shipping_address_collection``. So ``total_details.amount_tax`` is
readable straight off the created session — no browser, no card entry, no
completed payment. The webhook write-back half of the gate *does* need a
completed payment and stays with the ``@stripe`` Playwright suite; what we
assert here is the part that decides what the customer is charged.

HOW THE FLAG STATE IS READ
--------------------------
Not from ``printenv`` and not from ``/etc/grove/.env`` — from behaviour. A
created session either carries ``automatic_tax.enabled`` or it does not, and
that *is* the flag as the running Odoo process sees it. Files can converge
while the process stays stale (GOL-1772: a container's env is fixed at create
time, so a plain ``restart`` changes nothing); observing the session shape
cannot be fooled that way, and it needs no SSH to the droplet.

NO SELF-SKIP
------------
``docs/stripe-tax-cutover.md`` is explicit: "do not gate the assertion on key
presence (no self-skip)". A missing credential, an unreachable QA, a 503
"Checkout is not configured yet" — every one of those is a **FAIL** with a
named reason, never a skip and never a green. Train #1's lesson was that a
green gate whose specs silently skipped is worse than a red one.

USAGE
    bash scripts/stripe-tax-gate.sh --dry-run          # plan + preconditions
    bash scripts/stripe-tax-gate.sh --gate 4           # flag must be ON (incl. settlement)
    bash scripts/stripe-tax-gate.sh --case settlement  # ship-time leg only (GOL-2910)
    bash scripts/stripe-tax-gate.sh --gate 3           # flag must be OFF
    bash scripts/stripe-tax-gate.sh --verdict          # merge both runs -> flip?
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict

# ── Expected tax ────────────────────────────────────────────────────────────
# WV is a 6% STATE rate. QA's e2e fixtures are deliberately bound to the
# state-only tax, not the 7% state+municipal group (GOL-2449, grove-odoo-modules
# #271) — so 6% is the number Stripe must return for a WV destination once the
# test-mode WV registration exists (GOL-2574). A different-but-plausible rate is
# reported as RATE_MISMATCH rather than NO_TAX so the two failures are not
# confused: NO_TAX means the registration is missing, RATE_MISMATCH means it is
# there but configured differently.
DEFAULT_WV_RATE_PCT = 6.0
# Stripe rounds per line; one cent of drift across a multi-line cart is arithmetic,
# not a compliance failure.
TAX_TOLERANCE_CENTS = 2

# GOL-2233: ONE flat $10 deposit for the whole order, no matter the cart size.
DEPOSIT_DOLLARS = 10.00

# Settlement leg (GOL-2910): the ACTUAL label cost the gate records on its hand
# label. Any positive number works — the assertion is relative to the shipped
# base Odoo ends up with, not to this figure.
DEFAULT_SETTLE_SHIPPING = 12.50
# Stripe's reusable test card token; attached to a throwaway test-mode Customer
# so the off-session balance capture has a saved card to charge.
STRIPE_TEST_CARD_PM = "pm_card_visa"
# A plain UA: Cloudflare in front of QA bot-blocks the default Python-urllib one.
USER_AGENT = "grove-stripe-tax-gate/1.0"

# RFC 2606 reserved TLD, so scripts/qa-test-data-cleanup.py reaps every order and
# partner this gate creates with zero false positives (RESERVED_TEST_SUFFIXES).
# Never use a deliverable address here: the gate creates real draft orders.
GATE_BUYER_EMAIL = "stripe-tax-gate@grove.invalid"
GATE_BUYER_NAME = "Stripe Tax Gate (QA)"
GATE_BUYER_PHONE = "+1-304-555-0142"

WV_ADDRESS = {
    "street": "1 Gauley Bridge Rd",
    "city": "Summersville",
    "state": "WV",
    "zip": "26651",
    "country": "US",
}
# Ohio: adjacent, no Grove nexus, so Stripe Tax must return $0 — the same outcome
# as today's Odoo `_apply_destination_tax`, which is the point of the assertion.
NON_WV_ADDRESS = {
    "street": "600 Front St",
    "city": "Marietta",
    "state": "OH",
    "zip": "45750",
    "country": "US",
}

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"


@dataclass
class CaseResult:
    name: str
    gate: str
    status: str
    expect_flag: str
    observed_flag: str = "unknown"
    reasons: list = field(default_factory=list)
    observed: dict = field(default_factory=dict)

    def fail(self, code: str, detail: str) -> "CaseResult":
        self.status = STATUS_FAIL
        self.reasons.append({"code": code, "detail": detail})
        return self


# ── Pure assertion logic (unit-tested in scripts/test_stripe_tax_gate.py) ───

def observed_flag_state(session: dict) -> str:
    """Read the tenant flag as the RUNNING Odoo applied it, off the session.

    ``automatic_tax`` is only ever requested when ``_stripe_tax_enabled(order)``
    is true AND the order is not a deposit, so its presence is a one-way signal:
    enabled => flag ON. Absent is ambiguous on a deposit order (see
    ``deposit_shape_ok``), which is exactly why the deposit case asserts on the
    charged amount instead.
    """
    auto = session.get("automatic_tax") or {}
    return "on" if auto.get("enabled") else "off"


def session_tax_cents(session: dict) -> int:
    return int(((session.get("total_details") or {}).get("amount_tax")) or 0)


def taxable_base_cents(session: dict, tax_cents: int) -> int:
    """The base Stripe taxed = what it collected minus the tax it added."""
    return int(session.get("amount_total") or 0) - tax_cents


def expected_tax_cents(base_cents: int, rate_pct: float) -> int:
    return int(round(base_cents * rate_pct / 100.0))


def observed_rate_pct(base_cents: int, tax_cents: int) -> float:
    if base_cents <= 0:
        return 0.0
    return round(tax_cents * 100.0 / base_cents, 3)


def has_explicit_wv_tax_line(line_items: list) -> bool:
    """True when the session carries grove_headless' own "Sales tax (WV)" line.

    ``_wv_tax_line_name`` renders either "Sales tax (WV)" or "WV Sales Tax (6%)"
    depending on whether a percentage resolved, so match on both shapes rather
    than one literal.
    """
    pat = re.compile(r"(sales\s*tax\s*\(wv\)|wv\s*sales\s*tax)", re.I)
    for li in line_items or []:
        name = ((li.get("price") or {}).get("product") or {}) if isinstance(li.get("price"), dict) else {}
        for candidate in (li.get("description"), name.get("name") if isinstance(name, dict) else None):
            if candidate and pat.search(str(candidate)):
                return True
    return False


def evaluate_wv_full(res: CaseResult, session: dict, line_items: list, rate_pct: float) -> CaseResult:
    """Gate 4a — flag ON, WV destination: Stripe computes the 6% state tax."""
    if res.observed_flag != "on":
        return res.fail(
            "FLAG_NOT_ON",
            "session carries no automatic_tax: the running Odoo sees "
            "GROVE_STRIPE_TAX_* as OFF. Flip it (RUNBOOK-module-upgrade.md Path A) "
            "and re-run; a container restart alone does NOT re-read the env.",
        )
    tax = session_tax_cents(session)
    base = taxable_base_cents(session, tax)
    res.observed.update(amount_tax_cents=tax, taxable_base_cents=base,
                        observed_rate_pct=observed_rate_pct(base, tax))
    if tax == 0:
        return res.fail(
            "NO_TAX",
            "Stripe returned $0 tax for a WV destination. The test-mode WV "
            "registration is almost certainly missing — Stripe Tax settings are "
            "per mode and do not inherit from live (GOL-2574).",
        )
    want = expected_tax_cents(base, rate_pct)
    if abs(tax - want) > TAX_TOLERANCE_CENTS:
        return res.fail(
            "RATE_MISMATCH",
            f"Stripe charged {tax}c on a {base}c base = "
            f"{observed_rate_pct(base, tax)}%, expected ~{rate_pct}% ({want}c). "
            "QA fixtures are bound to the WV STATE-only rate (GOL-2449).",
        )
    if has_explicit_wv_tax_line(line_items):
        return res.fail(
            "DOUBLE_TAX",
            "the session carries BOTH Stripe's automatic_tax and grove_headless' "
            "own 'Sales tax (WV)' line — the customer would be taxed twice.",
        )
    return res


def evaluate_nonwv_full(res: CaseResult, session: dict, line_items: list) -> CaseResult:
    """Gate 4b — flag ON, no-nexus destination: Stripe must return $0."""
    if res.observed_flag != "on":
        return res.fail("FLAG_NOT_ON", "session carries no automatic_tax; flag reads OFF.")
    tax = session_tax_cents(session)
    res.observed.update(amount_tax_cents=tax)
    if tax != 0:
        return res.fail(
            "UNEXPECTED_TAX",
            f"Stripe charged {tax}c tax for an OH destination where Grove has no "
            "registered nexus. Either a stray registration exists in test mode or "
            "the ship-to did not reach the Stripe Customer.",
        )
    if has_explicit_wv_tax_line(line_items):
        return res.fail("DOUBLE_TAX", "WV tax line present on a non-WV order with Stripe Tax on.")
    return res


def evaluate_deposit(res: CaseResult, session: dict, order_resp: dict) -> CaseResult:
    """Gate 4c — flag ON, deposit order: Stripe Tax must stand DOWN at checkout.

    This case is NOT in the prose gate, and it is the one that matters most
    after Oct 15. ``checkout_session`` computes
    ``automatic_tax = tax_enabled and not is_deposit``, so a deposit order keeps
    Tax off at checkout and defers ALL tax to the ship-time settlement's
    ``/v1/tax/calculations`` (``_settle_*``). Train #2 promotes Oct 07 and the
    GOL-2233 season cutover is Oct 15: from Oct 16 every SHIPPED order containing
    bareroot takes the flat deposit instead of charging in full (potted-only and
    farm-pickup orders keep charging in full year-round). Bareroot is the
    dormant-season product, so within nine days of the flip the deposit path
    carries the dominant shape of nursery orders — and Gate 4a/4b never touch it.
    Without this case the flip would be licensed by a gate that skipped the code
    path about to carry the orders.
    """
    if not order_resp.get("has_preorder"):
        return res.fail(
            "NOT_A_DEPOSIT",
            "the cart did not trigger a deposit, so this case proves nothing. "
            "Point --variant-bareroot at a sold-out bareroot variant (or run "
            "after the Oct-15 cutover).",
        )
    if res.observed_flag == "on":
        return res.fail(
            "TAX_ON_DEPOSIT",
            "automatic_tax is enabled on a DEPOSIT session. A deposit is a flat "
            "$10 with tax deferred to settlement; taxing it here double-taxes the "
            "order when the balance settles.",
        )
    due = round(float(order_resp.get("amount_due_today") or 0.0), 2)
    res.observed.update(amount_due_today=due, amount_total=order_resp.get("amount_total"))
    if due != DEPOSIT_DOLLARS:
        return res.fail(
            "DEPOSIT_AMOUNT",
            f"charged ${due:.2f} today, expected the flat ${DEPOSIT_DOLLARS:.2f} "
            "per-order deposit (GOL-2233).",
        )
    if session_tax_cents(session) != 0:
        return res.fail("UNEXPECTED_TAX", "a deposit session must carry $0 tax.")
    return res


def evaluate_rollback_wv(res: CaseResult, session: dict, line_items: list) -> CaseResult:
    """Gate 3 — flag OFF, WV destination: Odoo's own tax line is back.

    Rollback is "flag off", so proving rollback means proving the OFF path is
    byte-identical to the pre-GOL-2568 checkout: no automatic_tax, and the
    explicit WV line grove_headless appends on the discounted base.
    """
    if res.observed_flag != "off":
        return res.fail(
            "FLAG_NOT_OFF",
            "automatic_tax is still enabled: the rollback has not taken effect on "
            "the running process. Remove the env line and recreate the container "
            "(--force-recreate); a restart reuses the old env.",
        )
    if session_tax_cents(session) != 0:
        return res.fail(
            "STRIPE_TAX_PRESENT",
            "Stripe reported tax on a flag-off session; tax must arrive as "
            "grove_headless' own line item, not from Stripe.",
        )
    if not has_explicit_wv_tax_line(line_items):
        return res.fail(
            "NO_ODOO_TAX_LINE",
            "no 'Sales tax (WV)' line on a WV order with the flag off — the "
            "rollback would under-collect WV sales tax. This is the failure that "
            "must block the flip.",
        )
    res.observed.update(odoo_wv_tax_line=True)
    return res


def _breakdown_entries(jurisdictions) -> list:
    """grove_stripe_tax_jurisdictions is the calc's raw ``tax_breakdown`` JSON."""
    if not jurisdictions:
        return []
    if isinstance(jurisdictions, str):
        try:
            jurisdictions = json.loads(jurisdictions)
        except json.JSONDecodeError:
            return []
    return [e for e in jurisdictions if isinstance(e, dict)] if isinstance(jurisdictions, list) else []


def breakdown_names_wv(entries: list) -> bool:
    """True when any taxed breakdown entry is West Virginia.

    A /v1/tax/calculations ``tax_breakdown`` entry carries the state under
    ``tax_rate_details.state``; Checkout-shaped breakdowns (and the module's
    test fixtures) carry a ``jurisdiction`` object instead — accept either, but
    only on an entry that actually taxed something.
    """
    for e in entries:
        if int(e.get("amount") or 0) <= 0:
            continue
        details = e.get("tax_rate_details") or {}
        juris = e.get("jurisdiction") or ((e.get("rate") or {}).get("jurisdiction")) or {}
        if isinstance(juris, str):
            juris = {"display_name": juris}
        if (str(details.get("state") or "").upper() == "WV"
                or str(juris.get("state") or "").upper() == "WV"
                or "west virginia" in str(juris.get("display_name") or "").lower()):
            return True
    return False


def breakdown_exclusive_cents(entries: list) -> int:
    """Stripe's ``tax_amount_exclusive`` == the sum of the non-inclusive entries."""
    return sum(int(e.get("amount") or 0) for e in entries if not e.get("inclusive"))


def evaluate_settlement(res: CaseResult, settle_status, order: dict, intent, rate_pct: float) -> CaseResult:
    """Gate 4d — flag ON, deposit order SHIPPED: Stripe Tax computes the balance.

    Gate 4c proves Stripe Tax stands DOWN on a deposit checkout. This proves the
    other half (GOL-2910): at ship, ``settle_order_at_ship`` asks
    ``/v1/tax/calculations`` for tax on the actual goods + actual shipping, and
    captures ``base + tax - $10`` off-session. After the Oct-15 cutover that is
    the dominant nursery order shape, so a flip licensed without it would be a
    flip whose main money path was never exercised.

    ``order`` is the sale.order read back after settlement; ``intent`` is the
    settlement PaymentIntent from Stripe (None when the order recorded none).
    """
    res.observed.update(settlement=settle_status,
                        checkout_status=order.get("grove_checkout_status"))
    if settle_status != "settled":
        return res.fail(
            "SETTLEMENT_STATUS",
            f"mark-shipped returned settlement={settle_status!r}, expected 'settled'. "
            "settlement_failed = no saved card / decline; not_applicable = the order "
            "was not deposit_paid; compliance_hold = consult mix (wrong variant).",
        )
    charged = round(float(order.get("grove_amount_charged_today") or 0.0), 2)
    res.observed["amount_charged_today"] = charged
    if charged != DEPOSIT_DOLLARS:
        return res.fail(
            "DEPOSIT_AMOUNT",
            f"checkout recorded ${charged:.2f} charged today, expected the flat "
            f"${DEPOSIT_DOLLARS:.2f} deposit (GOL-2233); the balance arithmetic is meaningless.",
        )
    entries = _breakdown_entries(order.get("grove_stripe_tax_jurisdictions"))
    tax_cents = int(round(float(order.get("grove_stripe_tax_amount") or 0.0) * 100))
    base_cents = int(round(float(order.get("amount_untaxed") or 0.0) * 100))
    res.observed.update(stripe_tax_cents=tax_cents, shipped_base_cents=base_cents,
                        observed_rate_pct=observed_rate_pct(base_cents, tax_cents))
    if not entries:
        # The module writes grove_stripe_tax_* ONLY when the calc returned. Empty
        # here means the balance was settled on Odoo's tax — either the flag is
        # off on the running process, or the calc failed and fell back (which
        # grove-odoo-modules#331 now alerts on in Discord).
        res.observed_flag = "off"
        return res.fail(
            "STRIPE_TAX_NOT_USED",
            "no grove_stripe_tax_jurisdictions on the settled order: the balance "
            "was computed on Odoo's tax, not Stripe's. Flag OFF on the running "
            "process, or the ship-time /v1/tax/calculations failed and fell back "
            "(look for a 'Stripe Tax FALLBACK' Discord alert / order chatter).",
        )
    res.observed_flag = "on"
    exclusive = breakdown_exclusive_cents(entries)
    if abs(exclusive - tax_cents) > 1:
        return res.fail(
            "TAX_RECORD_MISMATCH",
            f"grove_stripe_tax_amount={tax_cents}c but the recorded breakdown sums "
            f"to {exclusive}c (Stripe's tax_amount_exclusive).",
        )
    if tax_cents == 0:
        return res.fail(
            "NO_TAX",
            "Stripe Tax returned $0 at settlement for a WV ship-to. The test-mode "
            "WV registration is missing (GOL-2574).",
        )
    want = expected_tax_cents(base_cents, rate_pct)
    if abs(tax_cents - want) > TAX_TOLERANCE_CENTS:
        return res.fail(
            "RATE_MISMATCH",
            f"settlement tax {tax_cents}c on a {base_cents}c shipped base = "
            f"{observed_rate_pct(base_cents, tax_cents)}%, expected ~{rate_pct}% ({want}c).",
        )
    if not breakdown_names_wv(entries):
        return res.fail(
            "NO_WV_JURISDICTION",
            "the settlement tax breakdown names no West Virginia jurisdiction.",
        )
    expected_balance = base_cents + tax_cents - int(round(DEPOSIT_DOLLARS * 100))
    res.observed["expected_balance_cents"] = expected_balance
    if not intent:
        return res.fail("NO_SETTLEMENT_INTENT",
                        "the order is settled but carries no settlement PaymentIntent.")
    res.observed.update(captured_cents=int(intent.get("amount") or 0),
                        intent_status=intent.get("status"))
    if intent.get("status") != "succeeded":
        return res.fail("INTENT_STATUS",
                        f"settlement PaymentIntent is {intent.get('status')!r}, not 'succeeded'.")
    if abs(int(intent.get("amount") or 0) - expected_balance) > 1:
        return res.fail(
            "BALANCE_MISMATCH",
            f"captured {intent.get('amount')}c at ship, expected base {base_cents}c + "
            f"Stripe tax {tax_cents}c - ${DEPOSIT_DOLLARS:.0f} deposit = {expected_balance}c.",
        )
    return res


def aggregate(results: list) -> dict:
    """Fold case results into the flip decision.

    Conservative by construction: a gate is PASS only when every one of its
    cases ran AND passed, and the flip needs BOTH gates. Anything else — a
    missing case, an unreachable QA, a credential gap — leaves ``flip_allowed``
    false, which per Josh's ruling means the code still ships inert and the flip
    moves to Train #3. No further ruling needed, so an inconclusive run must
    never read as permission.
    """
    expected = {
        "4": {"wv_full", "nonwv_full", "deposit", "settlement"},
        "3": {"rollback_wv"},
    }
    by_gate: dict = {}
    for gate, names in expected.items():
        ran = {r.name: r for r in results if r.gate == gate}
        missing = sorted(names - set(ran))
        failed = sorted(n for n, r in ran.items() if r.status != STATUS_PASS)
        by_gate[gate] = {
            "status": STATUS_PASS if not missing and not failed else STATUS_FAIL,
            "missing_cases": missing,
            "failed_cases": failed,
        }
    flip = by_gate["3"]["status"] == STATUS_PASS and by_gate["4"]["status"] == STATUS_PASS
    return {
        "gate_3_rollback": by_gate["3"],
        "gate_4_amounts": by_gate["4"],
        "flip_allowed": flip,
        "ruling": (
            "Gates 3 and 4 both PASS — the GROVE_STRIPE_TAX_{TENANT} flip is "
            "cleared for the promote step (still a separate, board-approved action)."
            if flip else
            "Flip NOT cleared. Per Josh's GOL-2584 ruling the code ships INERT "
            "(flags OFF) and the flip moves to Train #3. No further ruling needed."
        ),
        "cases": [asdict(r) for r in results],
    }


# ── I/O ─────────────────────────────────────────────────────────────────────

def _post_json(url: str, body: dict, headers: dict, timeout: int = 45) -> tuple:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}


def _stripe_get(secret_key: str, path: str, params: str = "", timeout: int = 30) -> tuple:
    url = f"https://api.stripe.com/v1/{path}" + (f"?{params}" if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret_key}",
                                              "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}


def _stripe_post(secret_key: str, path: str, form: dict, timeout: int = 30) -> tuple:
    req = urllib.request.Request(
        f"https://api.stripe.com/v1/{path}", data=urllib.parse.urlencode(form).encode(),
        method="POST", headers={"Authorization": f"Bearer {secret_key}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}


class RpcError(Exception):
    pass


class OdooRpc:
    """Minimal Odoo /jsonrpc client (stdlib). Used ONLY by the settlement case to
    walk a QA deposit order to the ship event — steps the public checkout API
    cannot reach without a completed hosted payment."""

    def __init__(self, base: str, db: str, login: str, password: str):
        self.base, self.db, self.login, self.password = base, db, login, password
        self.uid = None

    def _call(self, service: str, method: str, *args):
        status, body = _post_json(f"{self.base}/jsonrpc", {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"service": service, "method": method, "args": list(args)},
        }, {})
        if status != 200:
            raise RpcError(f"HTTP {status}: {json.dumps(body)[:300]}")
        if body.get("error"):
            err = body["error"]
            raise RpcError(((err.get("data") or {}).get("message")) or err.get("message") or str(err)[:300])
        return body.get("result")

    def execute(self, model: str, method: str, args: list, kwargs: dict = None):
        if self.uid is None:
            self.uid = self._call("common", "login", self.db, self.login, self.password)
            if not self.uid:
                raise RpcError(f"login refused for {self.login!r} on db {self.db!r}")
        return self._call("object", "execute_kw", self.db, self.uid, self.password,
                          model, method, args, kwargs or {})


SETTLEMENT_READ_FIELDS = [
    "name", "grove_checkout_status", "grove_amount_charged_today", "amount_untaxed",
    "amount_tax", "grove_stripe_tax_amount", "grove_stripe_tax_jurisdictions",
    "grove_settlement_payment_intent",
]


CASES = {
    "wv_full": dict(gate="4", expect_flag="on", address=WV_ADDRESS, cart="instock",
                    label="Gate 4a — flag ON, WV ship-to: Stripe charges the 6% state tax"),
    "nonwv_full": dict(gate="4", expect_flag="on", address=NON_WV_ADDRESS, cart="instock",
                       label="Gate 4b — flag ON, OH ship-to: Stripe charges $0 (no nexus)"),
    "deposit": dict(gate="4", expect_flag="on", address=WV_ADDRESS, cart="bareroot",
                    label="Gate 4c — flag ON, deposit order: Tax stands down, flat $10"),
    "settlement": dict(gate="4", expect_flag="on", address=WV_ADDRESS, cart="bareroot",
                       label="Gate 4d — flag ON, deposit SHIPPED: Stripe Tax computes the balance"),
    "rollback_wv": dict(gate="3", expect_flag="off", address=WV_ADDRESS, cart="instock",
                        label="Gate 3 — flag OFF: Odoo's own WV tax line is back"),
}


class Runner:
    def __init__(self, args):
        self.odoo_base = (args.odoo_url or os.environ.get("QA_ODOO_URL") or "").rstrip("/")
        self.api_key = args.api_key or os.environ.get("QA_ODOO_API_KEY") or ""
        self.tenant = args.tenant
        self.stripe_key = args.stripe_key or os.environ.get("STRIPE_TEST_SECRET_KEY") or ""
        self.variants = {"instock": args.variant_instock, "bareroot": args.variant_bareroot}
        self.rate = args.wv_rate
        self.qty = args.quantity
        # Settlement leg only (GOL-2910): an Odoo login that may write sale.order
        # on QA, to simulate the completed deposit checkout and walk the order to
        # the ship event. getattr: older callers build a Namespace without them.
        self.rpc_db = getattr(args, "odoo_db", None) or os.environ.get("QA_ODOO_DB") or "odoo"
        self.rpc_login = getattr(args, "odoo_rpc_login", None) or os.environ.get("QA_ODOO_RPC_LOGIN") or ""
        self.rpc_password = getattr(args, "odoo_rpc_password", None) or os.environ.get("QA_ODOO_RPC_PASSWORD") or ""
        self.settle_shipping = getattr(args, "settle_shipping", None) or DEFAULT_SETTLE_SHIPPING

    def preconditions(self, names=()) -> list:
        """Everything that would otherwise surface as a confusing mid-run error."""
        problems = []
        if not self.odoo_base:
            problems.append("QA_ODOO_URL is unset (the QA Odoo base URL, e.g. https://odoo.qa.gatheringatthegrove.com)")
        elif "qa" not in self.odoo_base.lower():
            # QA and prod share the DB name `odoo`, so the hostname is the only
            # cheap discriminator. This gate CREATES ORDERS — pointing it at prod
            # would write test data into the system of record.
            problems.append(f"refusing to run against {self.odoo_base!r}: the host does not say 'qa'. "
                            "This gate creates real draft orders.")
        if not self.api_key:
            problems.append("QA_ODOO_API_KEY is unset (bearer key for /grove/api/v1/checkout/session)")
        if not self.stripe_key:
            problems.append("STRIPE_TEST_SECRET_KEY is unset")
        elif not self.stripe_key.startswith("sk_test"):
            problems.append("STRIPE_TEST_SECRET_KEY is not a test-mode key (must start with sk_test). "
                            "Refusing: a live key here would create real charges.")
        if not self.variants["instock"]:
            problems.append("--variant-instock is required (an in-stock, ships-now nursery variant id)")
        if not self.variants["bareroot"]:
            problems.append("--variant-bareroot is required (a sold-out bareroot variant id, for Gate 4c)")
        if "settlement" in names and not (self.rpc_login and self.rpc_password):
            problems.append("QA_ODOO_RPC_LOGIN / QA_ODOO_RPC_PASSWORD are unset (Gate 4d drives the "
                            "deposit order to the ship event over Odoo JSON-RPC)")
        return problems

    def run_case(self, name: str) -> CaseResult:
        if name == "settlement":
            return self.run_settlement()
        spec = CASES[name]
        res = CaseResult(name=name, gate=spec["gate"], status=STATUS_PASS,
                         expect_flag=spec["expect_flag"])
        return self._checkout_and_evaluate(name, spec, res)

    def _create_checkout(self, spec: dict) -> tuple:
        variant = self.variants[spec["cart"]]
        payload = {
            "contact": {"name": GATE_BUYER_NAME, "email": GATE_BUYER_EMAIL, "phone": GATE_BUYER_PHONE},
            "items": [{"variant_id": int(variant), "quantity": self.qty}],
            "shipping": dict(spec["address"]),
            "billing": dict(spec["address"]),
            "fulfillment": "ship",
            "success_url": "https://example.invalid/gate/ok",
            "cancel_url": "https://example.invalid/gate/cancel",
        }
        return _post_json(f"{self.odoo_base}/grove/api/v1/checkout/session", payload, self._api_headers())

    def _api_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "X-Grove-Tenant": self.tenant}

    def _checkout_and_evaluate(self, name: str, spec: dict, res: CaseResult) -> CaseResult:
        status, body = self._create_checkout(spec)
        if status != 200:
            # 503 "Checkout is not configured yet" means the tenant Stripe key is
            # not on the box — a precondition failure, reported as FAIL not skip.
            return res.fail("CHECKOUT_HTTP_%d" % status, json.dumps(body)[:300])
        res.observed["order_ref"] = body.get("order_ref")
        session_id = body.get("session_id")
        if not session_id:
            return res.fail("NO_SESSION_ID", "checkout response carried no session_id")
        st, session = _stripe_get(self.stripe_key, f"checkout/sessions/{session_id}",
                                  "expand[]=line_items")
        if st != 200:
            return res.fail("STRIPE_HTTP_%d" % st, json.dumps(session)[:300])
        line_items = ((session.get("line_items") or {}).get("data")) or []
        res.observed_flag = observed_flag_state(session)
        res.observed["session_id"] = session_id
        if name == "wv_full":
            return evaluate_wv_full(res, session, line_items, self.rate)
        if name == "nonwv_full":
            return evaluate_nonwv_full(res, session, line_items)
        if name == "deposit":
            return evaluate_deposit(res, session, body)
        return evaluate_rollback_wv(res, session, line_items)

    def run_settlement(self) -> CaseResult:
        """Gate 4d driver: deposit checkout -> saved test card -> wave -> hand
        label -> mark-shipped (which settles) -> read back order + intent.

        The hosted deposit payment itself is SIMULATED (the webhook's writes are
        made over RPC) because completing a Checkout Session needs a browser; the
        webhook half stays with the @stripe Playwright suite. Everything from the
        ship event on is the real production code path.
        """
        spec = CASES["settlement"]
        res = CaseResult(name="settlement", gate=spec["gate"], status=STATUS_PASS,
                         expect_flag=spec["expect_flag"])
        status, body = self._create_checkout(spec)
        if status != 200:
            return res.fail("CHECKOUT_HTTP_%d" % status, json.dumps(body)[:300])
        order_id, order_ref = body.get("order_id"), body.get("order_ref")
        res.observed.update(order_id=order_id, order_ref=order_ref)
        if not body.get("has_preorder"):
            return res.fail("NOT_A_DEPOSIT", "the bareroot cart did not trigger a deposit; "
                            "--variant-bareroot must be a SOLD-OUT bareroot variant.")
        if not order_id:
            return res.fail("NO_ORDER_ID", "checkout response carried no order_id")

        # A saved test card for the off-session balance capture.
        st, cus = _stripe_post(self.stripe_key, "customers", {
            "email": GATE_BUYER_EMAIL, "name": GATE_BUYER_NAME,
            "metadata[purpose]": "stripe-tax-gate", "metadata[order_ref]": order_ref or ""})
        if st != 200:
            return res.fail("STRIPE_HTTP_%d" % st, "create customer: " + json.dumps(cus)[:300])
        st, pm = _stripe_post(self.stripe_key, f"payment_methods/{STRIPE_TEST_CARD_PM}/attach",
                              {"customer": cus["id"]})
        if st != 200:
            return res.fail("STRIPE_HTTP_%d" % st, "attach test card: " + json.dumps(pm)[:300])

        rpc = OdooRpc(self.odoo_base, self.rpc_db, self.rpc_login, self.rpc_password)
        # Unique, alphanumeric (shippo_client.is_valid_tracking: 6-40 chars).
        tracking = f"9400GATE{int(time.time())}{order_id}"
        step = "simulate deposit webhook"
        try:
            rpc.execute("sale.order", "write", [[order_id], {
                "grove_checkout_status": "deposit_paid",
                "grove_stripe_customer": cus["id"],
                "grove_stripe_payment_method": pm["id"],
            }])
            step = "action_confirm"
            rpc.execute("sale.order", "action_confirm", [[order_id]])
            step = "assign wave"
            rpc.execute("sale.order", "action_grove_assign_wave", [[order_id]],
                        {"wave_ref": "stripe-tax-gate"})
            step = "record hand label"
            rpc.execute("sale.order", "action_grove_record_hand_label", [[order_id], tracking],
                        {"carrier": "USPS", "actual_cost": self.settle_shipping})
        except RpcError as exc:
            return res.fail("ODOO_RPC", f"{step}: {exc}")

        st, shipped = _post_json(f"{self.odoo_base}/grove/api/v1/orders/{order_id}/mark-shipped",
                                 {"actor": "stripe-tax-gate"}, self._api_headers())
        if st != 200:
            return res.fail("MARK_SHIPPED_HTTP_%d" % st, json.dumps(shipped)[:300])
        try:
            rows = rpc.execute("sale.order", "read", [[order_id]], {"fields": SETTLEMENT_READ_FIELDS})
        except RpcError as exc:
            return res.fail("ODOO_RPC", f"read back order: {exc}")
        order = rows[0] if rows else {}
        intent = None
        pi_id = order.get("grove_settlement_payment_intent")
        if pi_id:
            st, intent = _stripe_get(self.stripe_key, f"payment_intents/{pi_id}")
            if st != 200:
                return res.fail("STRIPE_HTTP_%d" % st, "read settlement intent: " + json.dumps(intent)[:300])
        return evaluate_settlement(res, shipped.get("settlement"), order, intent, self.rate)


def render(verdict: dict) -> str:
    lines = ["", "Stripe Tax cutover gate (GOL-2568 / GOL-2584)", "=" * 46]
    for c in verdict["cases"]:
        mark = "PASS" if c["status"] == STATUS_PASS else "FAIL"
        lines.append(f"[{mark}] {CASES[c['name']]['label']}")
        lines.append(f"        flag expected={c['expect_flag']} observed={c['observed_flag']}")
        for r in c["reasons"]:
            lines.append(f"        -> {r['code']}: {r['detail']}")
    for key, title in (("gate_3_rollback", "Gate 3 (rollback by flag-off)"),
                       ("gate_4_amounts", "Gate 4 (amounts asserted)")):
        g = verdict[key]
        extra = ""
        if g["missing_cases"]:
            extra += f" missing={','.join(g['missing_cases'])}"
        if g["failed_cases"]:
            extra += f" failed={','.join(g['failed_cases'])}"
        lines.append(f"{title}: {g['status']}{extra}")
    lines += [f"FLIP ALLOWED: {verdict['flip_allowed']}", verdict["ruling"], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run Stripe Tax Gates 3/4 against a live QA.")
    p.add_argument("--gate", choices=["3", "4"], help="which gate's cases to run")
    p.add_argument("--case", action="append", choices=sorted(CASES), help="run specific case(s)")
    p.add_argument("--dry-run", action="store_true", help="plan + preconditions only; creates nothing")
    p.add_argument("--odoo-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--stripe-key", default=None)
    p.add_argument("--tenant", default="nursery")
    p.add_argument("--variant-instock", default=os.environ.get("GATE_VARIANT_INSTOCK"))
    p.add_argument("--variant-bareroot", default=os.environ.get("GATE_VARIANT_BAREROOT"))
    p.add_argument("--wv-rate", type=float, default=DEFAULT_WV_RATE_PCT)
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--odoo-db", default=None, help="QA Odoo DB for the settlement case (QA_ODOO_DB, default odoo)")
    p.add_argument("--odoo-rpc-login", default=None)
    p.add_argument("--odoo-rpc-password", default=None)
    p.add_argument("--settle-shipping", type=float, default=None,
                   help=f"actual label cost the settlement case records (default {DEFAULT_SETTLE_SHIPPING})")
    p.add_argument("--merge", action="append", default=[],
                   help="verdict JSON from an earlier run, to fold in (lets gate 4 and "
                        "gate 3 run either side of the operator's flag flip)")
    p.add_argument("--json-out", default=None, help="write the verdict JSON here")
    args = p.parse_args(argv)

    runner = Runner(args)
    names = list(args.case or [])
    if args.gate:
        names += [n for n, s in CASES.items() if s["gate"] == args.gate and n not in names]
    if not names and not args.merge:
        names = sorted(CASES)

    problems = runner.preconditions(names)
    if args.dry_run:
        print("DRY RUN — nothing will be created.")
        print(f"  QA Odoo : {runner.odoo_base or '(unset)'}  tenant={runner.tenant}")
        print(f"  Stripe  : {'sk_test…' if runner.stripe_key.startswith('sk_test') else '(unset/not test)'}")
        print("  cases   :")
        for n in names:
            print(f"    - {CASES[n]['label']}")
        if problems:
            print("\n  BLOCKED — unmet preconditions:")
            for prob in problems:
                print(f"    * {prob}")
            return 2
        print("\n  preconditions OK.")
        return 0

    results = []
    for payload_path in args.merge:
        with open(payload_path) as fh:
            for c in json.load(fh).get("cases", []):
                results.append(CaseResult(**c))
    if names:
        if problems:
            # No self-skip: unmet preconditions become a FAIL per requested case,
            # so the verdict is red rather than silently short.
            for n in names:
                r = CaseResult(name=n, gate=CASES[n]["gate"], status=STATUS_PASS,
                               expect_flag=CASES[n]["expect_flag"])
                for prob in problems:
                    r.fail("PRECONDITION", prob)
                results.append(r)
        else:
            for n in names:
                results.append(runner.run_case(n))

    verdict = aggregate(results)
    print(render(verdict))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(verdict, fh, indent=2)
        print(f"verdict written to {args.json_out}")
    return 0 if verdict["flip_allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
