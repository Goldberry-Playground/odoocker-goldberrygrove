#!/usr/bin/env python3
"""
prod-reconfig — idempotent, assert-not-assume post-restore reconfiguration of a
promoted Odoo database (GOL-1329).

WHY THIS EXISTS
---------------
A QA→prod data promotion restores the QA database *bit for bit* (see
scripts/promote-db.sh + docs/runbooks/qa-to-prod-data-promotion.md). That dump
carries QA's environment-specific configuration baked into the DB:

  * `web.base.url` → the QA host (breaks every generated absolute URL / email
    link / OAuth callback if left pointing at qa.gatheringatthegrove.com).
  * `payment.provider` records in Stripe *test* state (checkout would charge
    nothing / reject live cards).
  * `ir.mail_server` pointed at the QA Mailgun sending domain.
  * `ir.cron` jobs left disabled by the freeze (step "freeze" in the runbook
    stops background writes; they must be re-enabled on prod).
  * publish / Stripe webhook config carrying QA endpoints.

Forgetting any one of these is a silent, launch-breaking miss — exactly the
class of "runbook footnote you can forget" that the promote-db.sh filestore
work turned into a structural step. This script makes the reconfig a *declared,
re-runnable, self-asserting* operation instead.

DESIGN — declarative + idempotent + schema-agnostic
---------------------------------------------------
The reconfig is driven by a JSON SPEC (see scripts/prod-reconfig.spec.example.json).
The spec is committed to git (it holds `${ENV_VAR}` references, never secret
values). Three kinds of entry:

  * config_parameters  — {key: value} written to ir.config_parameter (upsert via
                          set_param). Idempotent by construction.
  * record_writes      — [{model, domain, values, label}] ORM writes to whatever
                          rows match `domain`. Odoo writes are idempotent: a
                          second run writes the same values and changes nothing.
  * assertions         — [{model, domain, field?, expect?, count_expect?, label}]
                          read-back post-conditions. Every apply ends by running
                          ALL assertions; --check runs them alone. A failing
                          assertion exits non-zero — the script ASSERTS, it does
                          not assume the write "probably worked".

Secret values (Stripe live keys, Mailgun password, webhook secrets) are NEVER
in the spec or in code. The spec references them as `${STRIPE_LIVE_SECRET}` etc.
and this script resolves `${VAR}` from the process environment at run time
(inject with `op run --env-file=…`, like the other Grove scripts). A referenced
env var that is unset is a hard error — we never silently write an empty secret.

Because the live QA/prod schema (exact field names on payment.provider,
ir.mail_server, which crons the freeze disabled) is best read from the actual
restored DB, the spec is meant to be FINALISED during the scratch rehearsal:
run `--report` against the restored scratch DB to dump the current
environment-specific values, then fill the prod desired-state into the spec.

TARGET SAFETY
-------------
This writes PROD config values. Pointing it at the live QA DB would rewrite
QA's base URL to prod and break QA. Guards:

  * --target {scratch|prod} is REQUIRED and only labels/logs intent.
  * A `prod` target additionally requires PROD_RECONFIG_CONFIRM=yes in the env
    (a deliberate, auditable opt-in — mirrors "never bare-apply prod").
  * The XML-RPC URL is echoed on every run so a mis-set target is obvious in the
    log/evidence.

USAGE
-----
    # 1. During rehearsal: capture what the restored DB currently holds.
    ODOO_XMLRPC_URL=… ODOO_DB=… ODOO_LOGIN=… ODOO_API_KEY=… \
      python3 scripts/prod-reconfig.py --target scratch --spec <spec> --report

    # 2. Apply the desired prod state (writes, then asserts). Idempotent.
    op run --env-file=scripts/prod-reconfig.env.op -- \
      python3 scripts/prod-reconfig.py --target scratch --spec <spec> --apply

    # 3. Re-verify post-conditions any time (no writes):
    python3 scripts/prod-reconfig.py --target scratch --spec <spec> --check

Env:
  ODOO_XMLRPC_URL   Odoo XML-RPC base (default http://odoo:8069 on-box)
  ODOO_DB           target database name
  ODOO_LOGIN        Odoo login (needs settings/config write rights for --apply)
  ODOO_API_KEY      that user's API key (password for XML-RPC)
  plus every ${VAR} the chosen spec references (Stripe/Mailgun/webhook/base-url).

Exit codes: 0 ok; 1 connection/auth/guard/spec error; 2 an assertion failed.
Stdlib only (xmlrpc.client, argparse, json, re, os). Progress → stderr;
--json emits a machine-readable summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xmlrpc.client

# ── env-ref resolution ────────────────────────────────────────────────────────

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def resolve_env_refs(value, env: dict) -> object:
    """Recursively replace ${VAR} in strings (and nested dict/list values) from env.

    A referenced var that is missing raises KeyError — we never write an empty
    secret. Non-string leaves pass through unchanged. A whole-string ref
    (``"${X}"``) preserves the resolved value's type only insofar as env values
    are strings; Odoo write() accepts string scalars for these config fields.
    """
    if isinstance(value, str):
        missing: list[str] = []

        def _sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name not in env or env[name] == "":
                missing.append(name)
                return ""
            return env[name]

        out = _ENV_REF.sub(_sub, value)
        if missing:
            raise KeyError(
                f"unresolved/empty env ref(s) {sorted(set(missing))} in spec value {value!r}"
            )
        return out
    if isinstance(value, list):
        return [resolve_env_refs(v, env) for v in value]
    if isinstance(value, dict):
        return {k: resolve_env_refs(v, env) for k, v in value.items()}
    return value


# ── XML-RPC client (mirrors scripts/qa-test-data-cleanup.py) ──────────────────

class OdooClient:
    """Thin authenticated Odoo XML-RPC client."""

    def __init__(self, url: str, db: str, login: str, key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.key = key
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, login, key, {})
        if not self.uid:
            raise RuntimeError("Odoo XML-RPC authentication failed (check ODOO_DB/LOGIN/API_KEY)")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, args, kwargs or {})


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


# ── spec application (client is any object exposing .call(...) — real or fake) ─

def get_config_param(client, key: str):
    """Read one ir.config_parameter value (None if unset)."""
    rows = client.call("ir.config_parameter", "search_read",
                       [[["key", "=", key]]], {"fields": ["value"], "limit": 1})
    return rows[0]["value"] if rows else None


def set_config_param(client, key: str, value: str) -> None:
    """Upsert an ir.config_parameter (idempotent — sets the SAME value on re-run)."""
    client.call("ir.config_parameter", "set_param", [key, value])


def apply_config_parameters(client, params: dict) -> list[dict]:
    changes = []
    for key, value in params.items():
        before = get_config_param(client, key)
        set_config_param(client, str(key), str(value))
        changes.append({"key": key, "before": before, "after": str(value),
                        "changed": before != str(value)})
        flag = "→ set" if before != str(value) else "= unchanged"
        _log(f"  config_parameter[{key}] {flag} ({str(value)!r})")
    return changes


def apply_record_writes(client, writes: list) -> list[dict]:
    results = []
    for w in writes:
        model, domain, values = w["model"], w["domain"], w["values"]
        label = w.get("label", model)
        ids = client.call(model, "search", [domain])
        if ids:
            client.call(model, "write", [ids, values])
        _log(f"  record_write[{label}] {model} matched {len(ids)} row(s) ← {values}")
        results.append({"label": label, "model": model, "matched": len(ids), "values": values})
    return results


def run_assertions(client, assertions: list) -> tuple[list[dict], int]:
    """Run every assertion. Returns (results, failures). Never raises on a failed
    assertion — the caller decides the exit code — but DOES surface each verdict."""
    results, failures = [], 0
    for a in assertions:
        model, domain = a["model"], a["domain"]
        label = a.get("label", model)
        ok, detail = True, ""

        if "count_expect" in a:
            got = client.call(model, "search_count", [domain])
            ok = got == a["count_expect"]
            detail = f"count={got} expect={a['count_expect']}"
        elif "field" in a and "expect" in a:
            field = a["field"]
            rows = client.call(model, "search_read", [domain], {"fields": [field], "limit": 1})
            if not rows:
                ok, detail = False, f"no row matched {domain}"
            else:
                got = rows[0].get(field)
                ok = got == a["expect"]
                detail = f"{field}={got!r} expect={a['expect']!r}"
        elif "min_count" in a:
            got = client.call(model, "search_count", [domain])
            ok = got >= a["min_count"]
            detail = f"count={got} min_expect={a['min_count']}"
        else:
            ok, detail = False, "malformed assertion (need count_expect | field+expect | min_count)"

        verdict = "PASS" if ok else "FAIL"
        _log(f"  assert[{label}] {verdict}: {detail}")
        results.append({"label": label, "ok": ok, "detail": detail})
        if not ok:
            failures += 1
    return results, failures


def report_current(client, spec: dict) -> dict:
    """Dump the DB's current values for every key/target the spec touches, so an
    operator can see exactly what the restored DB holds before finalising the
    desired prod state. Read-only."""
    out: dict = {"config_parameters": {}, "record_targets": [], "assertion_targets": []}
    for key in spec.get("config_parameters", {}):
        out["config_parameters"][key] = get_config_param(client, key)
    for w in spec.get("record_writes", []):
        rows = client.call(w["model"], "search_read", [w["domain"]],
                           {"fields": list(w["values"].keys()), "limit": 20})
        out["record_targets"].append({"label": w.get("label", w["model"]),
                                       "model": w["model"], "domain": w["domain"], "rows": rows})
    for a in spec.get("assertions", []):
        fields = [a["field"]] if "field" in a else []
        rows = client.call(a["model"], "search_read", [a["domain"]],
                           {"fields": fields, "limit": 20}) if fields else []
        count = client.call(a["model"], "search_count", [a["domain"]])
        out["assertion_targets"].append({"label": a.get("label", a["model"]),
                                         "model": a["model"], "domain": a["domain"],
                                         "count": count, "rows": rows})
    return out


# ── cli ───────────────────────────────────────────────────────────────────────

def load_spec(path: str, env: dict) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return resolve_env_refs(raw, env)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Idempotent, self-asserting post-restore Odoo reconfig.")
    ap.add_argument("--spec", required=True, help="path to the reconfig JSON spec")
    ap.add_argument("--target", required=True, choices=("scratch", "prod"),
                    help="intent label; 'prod' additionally requires PROD_RECONFIG_CONFIRM=yes")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="dump current DB values (read-only)")
    mode.add_argument("--apply", action="store_true", help="apply writes, then run all assertions")
    mode.add_argument("--check", action="store_true", help="run assertions only (no writes)")
    ap.add_argument("--json", action="store_true", help="machine-readable summary to stdout")
    return ap


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv[1:])
    env = dict(os.environ)

    if args.target == "prod" and env.get("PROD_RECONFIG_CONFIRM") != "yes":
        _log("ERROR: --target prod requires PROD_RECONFIG_CONFIRM=yes in the env (auditable opt-in).")
        return 1

    url = env.get("ODOO_XMLRPC_URL", "http://odoo:8069")
    db, login = env.get("ODOO_DB", ""), env.get("ODOO_LOGIN", "")
    key = env.get("ODOO_API_KEY") or env.get("SYNTHETIC_ODOO_API_KEY", "")
    missing = [n for n, v in (("ODOO_DB", db), ("ODOO_LOGIN", login), ("ODOO_API_KEY", key)) if not v]
    if missing:
        _log(f"ERROR: missing required env {missing}")
        return 1

    # --report is read-only and never resolves secret refs (so you can inspect a
    # scratch DB without the prod secrets in your env). apply/check need the refs.
    try:
        if args.report:
            with open(args.spec, "r", encoding="utf-8") as fh:
                spec = json.load(fh)  # unresolved — report reads DB, not spec values
        else:
            spec = load_spec(args.spec, env)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        _log(f"ERROR: spec load/resolve failed: {exc}")
        return 1

    _log(f"== prod-reconfig ==  target={args.target}  db={db}  url={url}")

    try:
        client = OdooClient(url, db, login, key)
    except (OSError, xmlrpc.client.Fault, RuntimeError) as exc:
        _log(f"ERROR: cannot connect to Odoo at {url}: {exc}")
        return 1

    summary: dict = {"target": args.target, "db": db, "url": url, "mode": None}

    if args.report:
        current = report_current(client, spec)
        summary["mode"] = "report"
        summary["current"] = current
        _log("  (read-only) current environment-specific values dumped.")
        if args.json:
            print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.apply:
        summary["mode"] = "apply"
        summary["config_changes"] = apply_config_parameters(client, spec.get("config_parameters", {}))
        summary["record_writes"] = apply_record_writes(client, spec.get("record_writes", []))

    # apply always ends with a full assertion pass; --check runs it standalone.
    results, failures = run_assertions(client, spec.get("assertions", []))
    summary["mode"] = summary["mode"] or "check"
    summary["assertions"] = results
    summary["assertion_failures"] = failures

    if args.json:
        print(json.dumps(summary, indent=2, default=str))

    if failures:
        _log(f"❌ {failures} assertion(s) FAILED — reconfig post-conditions not met. DO NOT cut over.")
        return 2
    _log("✅ all reconfig post-conditions hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
