#!/usr/bin/env python3
"""check_stripe_key_scopes.py -- verify a Stripe restricted key's scope.

WHY THIS EXISTS (GOL-973 / runbook §4)
--------------------------------------
The prod checkout backend needs a *dedicated* restricted key (rk_live_ / rk_test_)
scoped to ONLY the operations grove_headless actually performs:

  - Checkout Sessions : WRITE  (POST /v1/checkout/session creates the session)
  - PaymentIntents    : READ   (reconciliation on the session's payment_intent)
  - Webhook Endpoints : READ    (optional; the runtime verifies with whsec_,
                                 but registering/inspecting the endpoint uses it)

Stripe's dashboard "restricted key" presets are *over-broad* by default (they
hand out Customers/Charges/Balance read, etc.). A leaked over-broad key exposes
far more than checkout. This script proves, BEFORE the key is wired into
`.env.op`, that:

  1. the REQUIRED capability (Checkout Sessions write) is present, and
  2. NONE of the FORBIDDEN capabilities (customer/charge/balance/payout/
     transfer/invoice/product reads) are reachable.

It is behavioral: Stripe does not expose a key's grant list, so we probe live
endpoints and read the HTTP status. All probes are side-effect free:
  - forbidden probes are GET ...?limit=1 (read-only), and
  - the one write probe is POST /v1/checkout/sessions with an EMPTY body, which
    Stripe rejects at validation (HTTP 400 "Missing required param") BEFORE
    creating anything. A 403 there means the key lacks Checkout write.

SECRET HANDLING
---------------
The key is NEVER accepted on argv (it would leak via ps/shell history/CI logs)
and is NEVER printed. Pass it by reference:
  --op-ref 'op://Grove Prod/<item-uuid>/<field-id>'   (resolved via `op read`)
  --env   STRIPE_KEY_TO_CHECK                         (read from the environment)
Only a masked fingerprint (prefix + last 4) is ever emitted.

EXIT CODES
----------
  0  PASS  -- required present, all forbidden denied
  1  FAIL  -- missing a required scope, or an over-broad scope is reachable
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
#   kind="required-write" : must be reachable past the permission layer (400/200)
#   kind="expected-read"  : allowed but not required; reported, never fails a run
#   kind="forbidden-read" : must be permission-denied (403); 200 => over-broad
PROBES = [
    ("Checkout Sessions (write)", "POST", "/v1/checkout/sessions", "required-write"),
    ("PaymentIntents (read)",     "GET",  "/v1/payment_intents?limit=1", "expected-read"),
    ("Webhook Endpoints (read)",  "GET",  "/v1/webhook_endpoints?limit=1", "expected-read"),
    ("Customers (read)",          "GET",  "/v1/customers?limit=1", "forbidden-read"),
    ("Charges (read)",            "GET",  "/v1/charges?limit=1", "forbidden-read"),
    ("Balance (read)",            "GET",  "/v1/balance", "forbidden-read"),
    ("Payouts (read)",            "GET",  "/v1/payouts?limit=1", "forbidden-read"),
    ("Transfers (read)",          "GET",  "/v1/transfers?limit=1", "forbidden-read"),
    ("Invoices (read)",           "GET",  "/v1/invoices?limit=1", "forbidden-read"),
    ("Products (read)",           "GET",  "/v1/products?limit=1", "forbidden-read"),
]


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
            return True, "GRANTED ✓ (required)"
        if denied:
            return False, "DENIED ✗ (required scope MISSING)"
        return None, "unexpected status %s" % status
    if kind == "expected-read":
        if granted:
            return True, "granted (ok, expected)"
        if denied:
            return True, "denied (acceptable — runtime uses whsec/none)"
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
    src = ap.add_argument_group("key source (exactly one; never on argv)")
    src.add_argument("--op-ref", help="1Password ref, e.g. 'op://Grove Prod/<item>/<field>'")
    src.add_argument("--env", help="name of an env var holding the key")
    args = ap.parse_args()

    key = resolve_key(args)
    if not (key.startswith("rk_") or key.startswith("sk_")):
        sys.exit("ERROR: resolved value is not a Stripe secret/restricted key "
                 "(expected rk_/sk_ prefix). Refusing to probe.")
    live = key.startswith("rk_live_") or key.startswith("sk_live_")
    print("Stripe scope check — key %s  (%s)"
          % (fingerprint(key), "LIVE" if live else "test"))
    print("-" * 62)

    required_ok = True
    forbidden_ok = True
    aborted = None
    for label, method, path, kind in PROBES:
        status, snippet = probe(key, method, path)
        ok, verdict = classify(kind, status)
        line = "  %-28s %-5s -> %s" % (label, str(status), verdict)
        print(line)
        if ok is None:
            aborted = verdict
            break
        if kind == "required-write" and not ok:
            required_ok = False
        if kind == "forbidden-read" and not ok:
            forbidden_ok = False

    print("-" * 62)
    if aborted:
        print("VERDICT: ERROR — %s" % aborted)
        return 2
    if required_ok and forbidden_ok:
        print("VERDICT: PASS — key is scoped to checkout only. Safe to wire.")
        return 0
    reasons = []
    if not required_ok:
        reasons.append("missing required Checkout Sessions write scope")
    if not forbidden_ok:
        reasons.append("OVER-BROAD: reachable scopes beyond checkout+webhook")
    print("VERDICT: FAIL — %s. Do NOT wire; re-mint with a narrower key."
          % "; ".join(reasons))
    return 1


if __name__ == "__main__":
    sys.exit(main())
