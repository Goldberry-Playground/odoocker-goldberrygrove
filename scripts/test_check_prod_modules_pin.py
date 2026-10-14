#!/usr/bin/env python3
"""Regression tests for scripts/check_prod_modules_pin.py (GOL-2580).

NO NETWORK. Every prod/GitHub call is stubbed. These pin the properties that
decide whether the nightly watcher is trustworthy:

  read-only-by-construction   _execute_kw refuses any method but search_read,
                              so a nightly cron holding a prod credential can
                              never mutate prod.
  fail-closed-no-credential   empty PROD_ODOO_LOGIN/SECRET exits 3 (ALERT),
                              NOT 0. A watcher that goes green because its
                              credential vanished is the anti-pattern this
                              issue exists to kill.
  drift-detected              committed version != live version exits 1 and the
                              output names BOTH versions and the reconcile
                              command (the success condition on GOL-2580).
  match-is-silent             equal versions exit 0.
  series-normalisation        a short manifest version ("1.53.0") compares equal
                              to Odoo's stored "19.0.1.53.0" -- otherwise every
                              single night is a false alarm.
  transient-vs-config         unreachable host exits 2 (retry once); refused
                              credential and a 403 exit 3 (never retry).
  waf-403-is-config           Cloudflare 403s the default python-urllib UA; that
                              must not be mistaken for a transient blip.
  credential-never-printed    the secret appears in no line of output on the
                              drift, match or error paths.
  real-file-parses            the REAL committed prod variables.tf yields a
                              40-hex ref through the shared block parser.
  alert-path-decoupled        the nightly workflow loads DISCORD_OPS_WEBHOOK_URL
                              in its OWN 1Password step, BEFORE the Odoo
                              credential step. load-secrets-action resolves
                              refs atomically, so co-loading them means a
                              missing/rotated Odoo item takes the webhook down
                              with it and the BROKEN alert never fires (review
                              GOL-2624 on PR #752).

    python3 scripts/test_check_prod_modules_pin.py
"""

from __future__ import annotations

import io
import os
import sys
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import check_prod_modules_pin as cp  # noqa: E402

REAL_TF = os.path.join(
    _ROOT, "infra", "terraform", "environments", "production", "variables.tf"
)

SECRET = "s3cr3t-api-key-do-not-log"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class _Harness:
    """Stubs every outbound call in the module and captures stdout/stderr."""

    def __init__(self, *, live_version="19.0.1.53.0", manifest_version="19.0.1.53.0",
                 rpc_error=None, manifest_error=None, candidates=None):
        self.live_version = live_version
        self.manifest_version = manifest_version
        self.rpc_error = rpc_error
        self.manifest_error = manifest_error
        self.candidates = candidates
        self.out = ""

    def __enter__(self):
        self._orig = {n: getattr(cp, n) for n in
                      ("_rpc", "manifest_version_at", "resolve_live_candidates")}

        def fake_rpc(url, service, method, args, timeout):
            if self.rpc_error:
                raise self.rpc_error
            if (service, method) == ("common", "authenticate"):
                return 2
            if (service, method) == ("object", "execute_kw"):
                return [{"name": "grove_headless",
                         "installed_version": self.live_version,
                         "state": "installed",
                         "write_date": "2026-09-23 21:09:47"}]
            raise AssertionError(f"unexpected rpc {service}.{method}")

        def fake_manifest(repo, sha, module, timeout=30, token=""):
            if self.manifest_error:
                raise self.manifest_error
            return self.manifest_version

        def fake_candidates(repo, module, live_version, timeout=30, token="", max_probe=25):
            return (self.candidates or []), ""

        cp._rpc = fake_rpc
        cp.manifest_version_at = fake_manifest
        cp.resolve_live_candidates = fake_candidates

        self._buf_out, self._buf_err = io.StringIO(), io.StringIO()
        self._so, self._se = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._buf_out, self._buf_err
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self._so, self._se
        self.out = self._buf_out.getvalue() + self._buf_err.getvalue()
        for n, v in self._orig.items():
            setattr(cp, n, v)
        return False


def run_main(env: dict, argv: list[str], **hk) -> tuple[int, str]:
    saved = {k: os.environ.get(k) for k in
             ("PROD_ODOO_LOGIN", "PROD_ODOO_SECRET", "PROD_ODOO_URL",
              "PROD_ODOO_DB", "MODULES_REPO", "GH_RAW_TOKEN")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        with _Harness(**hk) as h:
            rc = cp.main(argv)
        return rc, h.out
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


CREDS = {"PROD_ODOO_LOGIN": "ci-drift@goldberrygrove.farm", "PROD_ODOO_SECRET": SECRET}


def test_read_only_by_construction() -> None:
    print("read-only-by-construction")
    for method in ("write", "create", "unlink", "search", "execute", "button_immediate_upgrade"):
        try:
            cp._execute_kw("https://x", "odoo", 2, SECRET, "ir.module.module",
                           method, [], {}, 5)
            check(f"refuses {method}", False, "(no exception raised)")
        except cp.ConfigError as exc:
            check(f"refuses {method}", "read-only by construction" in str(exc))
        except Exception as exc:  # noqa: BLE001
            check(f"refuses {method}", False, f"(wrong exception {exc!r})")
    check("allow-list is exactly search_read", cp.ALLOWED_METHODS == {"search_read"},
          f"(got {cp.ALLOWED_METHODS})")


def test_fail_closed_no_credential() -> None:
    print("fail-closed-no-credential")
    for env, label in (({}, "both empty"),
                       ({"PROD_ODOO_LOGIN": "x"}, "secret empty"),
                       ({"PROD_ODOO_SECRET": SECRET}, "login empty")):
        rc, out = run_main(env, ["--file", REAL_TF])
        check(f"{label} -> exit 3 (ALERT, not green)", rc == cp.EXIT_BROKEN_CONFIG,
              f"(got {rc})")
        check(f"{label} -> says BLIND", "BLIND" in out)


def test_drift_detected() -> None:
    print("drift-detected")
    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       live_version="19.0.1.53.0", manifest_version="19.0.1.40.0",
                       candidates=["19b1cbfbed3c18ff6ee5cfea8985b36064011e15"])
    check("exit 1", rc == cp.EXIT_DRIFT, f"(got {rc})")
    check("names the live version", "19.0.1.53.0" in out)
    check("names the committed version", "19.0.1.40.0" in out)
    check("says BACKWARD", "BACKWARD" in out)
    check("names the reconcile workflow", "reconcile-modules-pin.yml" in out)
    check("gives a copy-pasteable -f modules_sha",
          "-f modules_sha=19b1cbfbed3c18ff6ee5cfea8985b36064011e15" in out)
    check("warns against a bare rebuild", "-var custom_modules_ref" in out)

    # Committed AHEAD of live is also drift, with the opposite wording.
    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       live_version="19.0.1.40.0", manifest_version="19.0.1.53.0")
    check("ahead -> exit 1", rc == cp.EXIT_DRIFT, f"(got {rc})")
    check("ahead -> says AHEAD", "AHEAD" in out)


def test_match_is_silent() -> None:
    print("match-is-silent")
    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       live_version="19.0.1.53.0", manifest_version="19.0.1.53.0")
    check("exit 0", rc == cp.EXIT_OK, f"(got {rc})")
    check("says MATCH", "MATCH" in out)
    check("no DRIFT line", "DRIFT:" not in out)
    check("states the version-not-SHA caveat", "no version-level rollback" in out)


def test_series_normalisation() -> None:
    print("series-normalisation")
    check("short manifest gains the series", cp.normalize("1.53.0") == "19.0.1.53.0",
          f"(got {cp.normalize('1.53.0')})")
    check("full version untouched", cp.normalize("19.0.1.53.0") == "19.0.1.53.0")
    rc, _ = run_main(CREDS, ["--file", REAL_TF],
                     live_version="19.0.1.53.0", manifest_version="1.53.0")
    check("short vs stored compares EQUAL (else nightly false alarm)",
          rc == cp.EXIT_OK, f"(got {rc})")


def test_transient_vs_config() -> None:
    print("transient-vs-config")
    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       rpc_error=cp.TransientError("prod JSON-RPC unreachable: timed out"))
    check("unreachable -> exit 2 (retry once)", rc == cp.EXIT_BROKEN_TRANSIENT, f"(got {rc})")

    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       rpc_error=cp.ConfigError("prod JSON-RPC error: Access Denied"))
    check("refused -> exit 3 (never retry)", rc == cp.EXIT_BROKEN_CONFIG, f"(got {rc})")

    rc, out = run_main(CREDS, ["--file", REAL_TF],
                       manifest_error=cp.ConfigError("__manifest__.py not found"))
    check("bogus committed SHA -> exit 3", rc == cp.EXIT_BROKEN_CONFIG, f"(got {rc})")

    rc, out = run_main(CREDS, ["--file", os.path.join(_ROOT, "no", "such", "file.tf")])
    check("missing variables.tf -> exit 3", rc == cp.EXIT_BROKEN_CONFIG, f"(got {rc})")


def test_waf_403_is_config() -> None:
    print("waf-403-is-config")
    orig = cp.urllib.request.urlopen

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    cp.urllib.request.urlopen = boom
    try:
        cp._rpc("https://odoo.example", "common", "authenticate", ["odoo", "u", SECRET, {}], 5)
        check("403 raises", False, "(no exception)")
    except cp.ConfigError as exc:
        check("403 -> ConfigError not TransientError", True)
        check("403 blames the WAF, not Odoo auth", "Cloudflare WAF" in str(exc))
        check("403 message carries no secret", SECRET not in str(exc))
    except Exception as exc:  # noqa: BLE001
        check("403 -> ConfigError not TransientError", False, f"(got {exc!r})")
    finally:
        cp.urllib.request.urlopen = orig

    check("an explicit User-Agent is set at all", bool(cp.USER_AGENT))
    check("UA is not the stdlib default", "Python-urllib" not in cp.USER_AGENT)


def test_credential_never_printed() -> None:
    print("credential-never-printed")
    cases = [
        ("drift", dict(live_version="19.0.1.53.0", manifest_version="19.0.1.40.0")),
        ("match", dict(live_version="19.0.1.53.0", manifest_version="19.0.1.53.0")),
        ("rpc-error", dict(rpc_error=cp.ConfigError("Access Denied for user"))),
        ("transient", dict(rpc_error=cp.TransientError("connection reset"))),
    ]
    for label, hk in cases:
        _, out = run_main(CREDS, ["--file", REAL_TF, "--json"], **hk)
        check(f"{label}: secret absent from output", SECRET not in out)


def test_real_file_parses() -> None:
    print("real-file-parses")
    ref = cp.committed_ref(REAL_TF)
    check("committed ref is 40-hex", bool(cp.re.fullmatch(r"[0-9a-f]{40}", ref)),
          f"(got {ref!r})")
    check("uses the same parser as the reconciler",
          cp._find_block.__module__ == "reconcile_modules_pin")


WORKFLOW = os.path.join(_ROOT, ".github", "workflows", "prod-modules-pin-drift.yml")


def _op_steps(path: str) -> list[dict]:
    """Split the workflow's steps and collect each one's `op://` env refs.

    Deliberately hand-rolled: `promotion-script-tests` runs bare `python3` with
    no pip install, so PyYAML is not available. We only need the step
    boundaries (`      - name:`) and the `op://` lines inside each.
    """
    steps: list[dict] = []
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("      - name:"):
            steps.append({"name": line.split(":", 1)[1].strip(), "refs": []})
        elif steps and "op://" in line and not line.lstrip().startswith("#"):
            var = line.strip().split(":", 1)[0]
            steps[-1]["refs"].append(var)
    return [s for s in steps if s["refs"]]


def test_alert_path_decoupled() -> None:
    print("alert-path-decoupled")
    steps = _op_steps(WORKFLOW)
    check("the workflow has 1Password steps at all", bool(steps))

    webhook = [s for s in steps if "DISCORD_OPS_WEBHOOK_URL" in s["refs"]]
    odoo = [s for s in steps if any(r.startswith("PROD_ODOO_") for r in s["refs"])]
    check("exactly one step loads the webhook", len(webhook) == 1,
          f"(got {len(webhook)})")
    check("exactly one step loads the Odoo credential", len(odoo) == 1,
          f"(got {len(odoo)})")
    if not (webhook and odoo):
        return

    # THE regression: load-secrets-action resolves every op:// ref atomically.
    # One unresolvable ref fails the step and exports NOTHING -- so a webhook
    # sharing a step with the Odoo item is silenced by a missing Odoo item, and
    # discord-status.sh then skips silently on the empty webhook (exit 0).
    check("webhook step loads ONLY the webhook", webhook[0]["refs"] == ["DISCORD_OPS_WEBHOOK_URL"],
          f"(loads {webhook[0]['refs']})")
    check("webhook step is NOT the Odoo credential step", webhook[0] is not odoo[0])
    check("webhook loads BEFORE the Odoo credential",
          steps.index(webhook[0]) < steps.index(odoo[0]))

    # The Odoo step must keep continue-on-error (it is what lets the job reach
    # the Discord step to report its own brokenness); the webhook step must not
    # need it, because a webhook that cannot load has nothing to report with.
    body = open(WORKFLOW, encoding="utf-8").read()
    odoo_block = body.split("- name: " + odoo[0]["name"], 1)[1]
    check("Odoo credential step keeps continue-on-error",
          "continue-on-error: true" in odoo_block.split("op://", 1)[0])


def main() -> int:
    for fn in (test_read_only_by_construction, test_fail_closed_no_credential,
               test_drift_detected, test_match_is_silent, test_series_normalisation,
               test_transient_vs_config, test_waf_403_is_config,
               test_credential_never_printed, test_real_file_parses,
               test_alert_path_decoupled):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
