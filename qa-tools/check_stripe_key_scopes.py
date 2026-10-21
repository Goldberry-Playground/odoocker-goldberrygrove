#!/usr/bin/env python3
"""check_stripe_key_scopes.py -- verify a Grove Stripe restricted key's scope.

WHY THIS EXISTS (GOL-973 / GOL-1763 / runbook §4)
--------------------------------------------------
Each LLC's Stripe account (At The Grove Nursery, GGG Woodworking, Goldberry
Grove) is driven by ONE restricted key (`rk_live_` / `rk_test_`) that the
`grove_headless` Odoo module reads per tenant via ``_tenant_secret_key`` and
feeds into the *same* controller code. These keys are NOT storefront-only — the
original narrow target (Checkout write + PaymentIntents read) was wrong and
would have broken live money flows (GOL-1763, Josh 2026-10-21). The storefront
(grove-sites) never holds the key: its ``/api/checkout/session`` routes forward
to grove_headless, which creates the Stripe objects server-side.

The key's MINIMUM scope is the set of resources those server-side call sites
actually touch. Derived from grove_headless/models/stripe_gateway.py +
controllers/main.py (GOL-1763 scope audit, 2026-10-21):

  REQUIRED (must be granted — a 403 breaks a money flow):
    - Checkout Sessions   : WRITE  POST /v1/checkout/sessions        create_checkout_session
    - Coupons             : WRITE  POST /v1/coupons                  create_coupon (promo discount, GOL-2088)
    - PaymentIntents      : WRITE  POST /v1/payment_intents          create_payment_intent (off-session ship settlement, GOL-2053)
                                   GET  /v1/payment_intents/:id       retrieve_payment_intent  (read ⊂ write)
                                   GET  /v1/payment_intents/search    search_payment_intents   (reconcile, GOL-3011)
    - Refunds             : WRITE  POST /v1/refunds                  create_refund (Printify cancel + tax refunds, GOL-3314/3074)
    - Customers           : WRITE  POST /v1/customers                create_customer (Stripe Tax ship-to carrier, GOL-2568)
                                   POST /v1/customers/:id             update_customer
                                   GET  /v1/customers                 find_customer_by_email   (read ⊂ write)
    - Tax                 : WRITE  POST /v1/tax/calculations         create_tax_calculation (ship-time tax, GOL-2233)
                                   POST /v1/tax/transactions/...      create_tax_transaction

  OPTIONAL (minimum is None; harmless if granted Read):
    - Webhook Endpoints   : NONE   runtime verifies with the whsec_ secret via local HMAC
                                   (verify_webhook_signature) — NO Stripe API call is made.

  FORBIDDEN (must be denied — nothing in grove_headless reads these; a 200 is
  the over-broad default-preset exposure we are narrowing away):
    - Charges, Balance, Payouts, Transfers, Invoices, Products   : NONE

All three LLC keys share this profile today because they drive the same
tenant-parameterized controller. KEY_PROFILES keeps them separate so a future
per-tenant divergence (e.g. a tenant that never deposits) is a one-line change.

PROBE SAFETY
------------
Behavioral: Stripe does not expose a key's grant list, so we probe live
endpoints and read the HTTP status. Every probe is side-effect free:
  - read probes are GET ...?limit=1 (nothing created), and
  - every WRITE probe is POST with an EMPTY body, which Stripe rejects at
    validation (HTTP 400 "Missing required param") BEFORE creating anything.
    A 403 there means the key lacks that write scope; a 400/200 means it has it.
  - Customers write cannot be probed empty (an empty POST /v1/customers SUCCEEDS
    and creates a junk customer), so Customers is probed as a READ FLOOR (GET,
    must not be 403). Grant it WRITE in the dashboard — the code needs write.

SECRET HANDLING
---------------
The key is NEVER accepted on argv (it would leak via ps/shell history/CI logs)
and is NEVER printed. Pass it by reference:
  --op-ref 'op://Grove Prod/<item-uuid>/<field-id>'   (resolved via `op read`)
  --env   STRIPE_KEY_TO_CHECK                         (read from the environment)
Only a masked fingerprint (prefix + last 4) is ever emitted.

EXIT CODES
----------
  0  PASS  -- every required scope present, every forbidden scope denied
  1  FAIL  -- a required scope is missing, or a forbidden scope is reachable
  2  ERROR -- bad key (401), resolution failure, or inconclusive probes
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

STRIPE_API = "https://api.stripe.com"

# (label, method, path, kind)
#   kind="required-write" : must be reachable past the permission layer (400/200); 403 => FAIL
#   kind="required-read"  : must not be permission-denied (403 => FAIL). A read floor:
#                           the resource the code also writes, but whose write cannot be
#                           probed without a side effect (Customers). Grant WRITE in the UI.
#   kind="expected-read"  : allowed but not required; reported, never fails a run
#   kind="forbidden-read" : must be permission-denied (403); 200 => over-broad => FAIL
#
# The shared Grove backend-key profile (all three LLC keys, 2026-10-21 audit).
BACKEND_PROFILE = [
    ("Checkout Sessions (write)", "POST", "/v1/checkout/sessions", "required-write"),
    ("Coupons (write)",           "POST", "/v1/coupons", "required-write"),
    ("PaymentIntents (write)",    "POST", "/v1/payment_intents", "required-write"),
    ("Refunds (write)",           "POST", "/v1/refunds", "required-write"),
    ("Tax calculations (write)",  "POST", "/v1/tax/calculations", "required-write"),
    ("Tax transactions (write)",  "POST", "/v1/tax/transactions/create_from_calculation", "required-write"),
    ("PaymentIntents (read)",     "GET",  "/v1/payment_intents?limit=1", "required-read"),
    ("Customers (read floor)",    "GET",  "/v1/customers?limit=1", "required-read"),
    ("Webhook Endpoints (read)",  "GET",  "/v1/webhook_endpoints?limit=1", "expected-read"),
    ("Charges (read)",            "GET",  "/v1/charges?limit=1", "forbidden-read"),
    ("Balance (read)",            "GET",  "/v1/balance", "forbidden-read"),
    ("Payouts (read)",            "GET",  "/v1/payouts?limit=1", "forbidden-read"),
    ("Transfers (read)",          "GET",  "/v1/transfers?limit=1", "forbidden-read"),
    ("Invoices (read)",           "GET",  "/v1/invoices?limit=1", "forbidden-read"),
    ("Products (read)",           "GET",  "/v1/products?limit=1", "forbidden-read"),
]

# Per-key expected sets. All three point at the same profile today (same code);
# split one out here if a tenant's exercised call sites ever diverge.
KEY_PROFILES = {
    "nursery": BACKEND_PROFILE,
    "ggg": BACKEND_PROFILE,
    "goldberry": BACKEND_PROFILE,
    "backend": BACKEND_PROFILE,
}


def resolve_key(args):
    if args.op_ref:
        try:
            out = subprocess.run(
                ["op", "read", args.op_ref],
                capture_output=True, text=True, check=True,
            )
            return out.stdout.strip()
        except FileNotFoundError:
            sys.exit("ERROR: `op` CLI not found on PATH; cannot resolve --op-ref")
        except subprocess.CalledProcessError as e:
            sys.exit("ERROR: `op read` failed for the given ref: %s" % e.stderr.strip())
    if args.env:
        val = os.environ.get(args.env)
        if not val:
            sys.exit("ERROR: env var %r is empty/unset" % args.env)
        return val.strip()
    sys.exit("ERROR: provide --op-ref or --env (never pass the key on argv)")


def fingerprint(key):
    if len(key) < 12:
        return "<short/invalid>"
    return "%s…%s" % (key[:8], key[-4:])


def probe(key, method, path):
    """Return (http_status:int | None, snippet:str)."""
    url = STRIPE_API + path
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Stripe-Version", "2024-06-20")
    if method == "POST":
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.getcode(), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
            j = json.loads(body)
            body = j.get("error", {}).get("message", "")[:120]
        except Exception:
            body = body[:120]
        return e.code, body
    except urllib.error.URLError as e:
        return None, "network error: %s" % e.reason


def classify(kind, status):
    """Return (ok:bool|None, verdict:str). None => inconclusive/abort."""
    if status == 401:
        return None, "INVALID KEY (401) — bad or revoked key"
    if status == 429:
        return None, "RATE LIMITED (429) — inconclusive, re-run"
    if status is None:
        return None, "NETWORK ERROR — inconclusive"

    granted = status in (200, 400)  # 400 = past permission layer into validation
    denied = status == 403

    if kind == "required-write":
        if granted:
            return True, "GRANTED ✓ (required write)"
        if denied:
            return False, "DENIED ✗ (required WRITE scope MISSING)"
        return None, "unexpected status %s" % status
    if kind == "required-read":
        if granted:
            return True, "granted ✓ (required; grant WRITE in dashboard)"
        if denied:
            return False, "DENIED ✗ (required READ scope MISSING)"
        return None, "unexpected status %s" % status
    if kind == "expected-read":
        if granted:
            return True, "granted (ok, optional)"
        if denied:
            return True, "denied (acceptable — minimum is None; runtime uses whsec)"
        return True, "status %s (informational)" % status
    if kind == "forbidden-read":
        if denied:
            return True, "denied ✓ (correctly restricted)"
        if status == 200:
            return False, "REACHABLE ✗ (OVER-BROAD — key can read this)"
        if status == 400:
            return False, "reachable ✗ (over-broad — passed permission layer)"
        return None, "unexpected status %s" % status
    return None, "unknown probe kind"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=sorted(KEY_PROFILES), default="backend",
                    help="which key's expected set to check (nursery|ggg|goldberry|backend; "
                         "all identical today — the shared grove_headless backend profile)")
    src = ap.add_argument_group("key source (exactly one; never on argv)")
    src.add_argument("--op-ref", help="1Password ref, e.g. 'op://Grove Prod/<item>/<field>'")
    src.add_argument("--env", help="name of an env var holding the key")
    args = ap.parse_args()

    probes = KEY_PROFILES[args.profile]
    key = resolve_key(args)
    if not (key.startswith("rk_") or key.startswith("sk_")):
        sys.exit("ERROR: resolved value is not a Stripe secret/restricted key "
                 "(expected rk_/sk_ prefix). Refusing to probe.")
    live = key.startswith("rk_live_") or key.startswith("sk_live_")
    print("Stripe scope check — profile %r — key %s  (%s)"
          % (args.profile, fingerprint(key), "LIVE" if live else "test"))
    print("-" * 68)

    required_ok = True
    forbidden_ok = True
    aborted = None
    for label, method, path, kind in probes:
        status, snippet = probe(key, method, path)
        ok, verdict = classify(kind, status)
        print("  %-28s %-5s -> %s" % (label, str(status), verdict))
        if ok is None:
            aborted = verdict
            break
        if kind in ("required-write", "required-read") and not ok:
            required_ok = False
        if kind == "forbidden-read" and not ok:
            forbidden_ok = False

    print("-" * 68)
    if aborted:
        print("VERDICT: ERROR — %s" % aborted)
        return 2
    if required_ok and forbidden_ok:
        print("VERDICT: PASS — key scoped to the grove_headless money paths only. Safe.")
        return 0
    reasons = []
    if not required_ok:
        reasons.append("missing a required scope (a money flow would 403)")
    if not forbidden_ok:
        reasons.append("OVER-BROAD: reachable scopes beyond what grove_headless uses")
    print("VERDICT: FAIL — %s." % "; ".join(reasons))
    return 1


if __name__ == "__main__":
    sys.exit(main())
