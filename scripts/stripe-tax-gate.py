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
    bash scripts/stripe-tax-gate.sh --gate 4           # flag must be ON
    bash scripts/stripe-tax-gate.sh --gate 3           # flag must be OFF
    bash scripts/stripe-tax-gate.sh --verdict          # merge both runs -> flip?
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
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
        "4": {"wv_full", "nonwv_full", "deposit"},
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
                                 headers={"Content-Type": "application/json", **headers})
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
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}


CASES = {
    "wv_full": dict(gate="4", expect_flag="on", address=WV_ADDRESS, cart="instock",
                    label="Gate 4a — flag ON, WV ship-to: Stripe charges the 6% state tax"),
    "nonwv_full": dict(gate="4", expect_flag="on", address=NON_WV_ADDRESS, cart="instock",
                       label="Gate 4b — flag ON, OH ship-to: Stripe charges $0 (no nexus)"),
    "deposit": dict(gate="4", expect_flag="on", address=WV_ADDRESS, cart="bareroot",
                    label="Gate 4c — flag ON, deposit order: Tax stands down, flat $10"),
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

    def preconditions(self) -> list:
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
        return problems

    def run_case(self, name: str) -> CaseResult:
        spec = CASES[name]
        res = CaseResult(name=name, gate=spec["gate"], status=STATUS_PASS,
                         expect_flag=spec["expect_flag"])
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
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Grove-Tenant": self.tenant}
        status, body = _post_json(f"{self.odoo_base}/grove/api/v1/checkout/session", payload, headers)
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

    problems = runner.preconditions()
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
