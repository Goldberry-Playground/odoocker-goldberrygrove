#!/usr/bin/env python3
"""
check_prod_modules_pin — detect drift between the COMMITTED prod
``custom_modules_ref`` default and the grove_headless version prod is actually
running, over a read-only HTTPS JSON-RPC call.

WHY THIS EXISTS (GOL-2580)
--------------------------
``digitalocean_droplet.odoo`` carries ``lifecycle { ignore_changes = [user_data,
monitoring] }``. Every prod modules promote / incident hand-edits
``/etc/grove/.env``'s ``CUSTOM_MODULES_REF`` on the live box with **no commit**,
so the committed ``var.custom_modules_ref`` default silently rots behind and the
next droplet REBUILD rolls prod backward.

That has now happened five times (``34cc4548``, ``22fbb71``, ``0b36ecfa``,
GOL-2134/GOL-2238, GOL-2579 — the last sat undetected for six days across a
money-path release). ``reconcile-modules-pin.yml`` (GOL-2281) makes the *fix*
one command; nothing ever **detected** the drift. A human noticing is not a
control. This script is the control.

THE DISCRIMINATOR
-----------------
The agent plane and GitHub Actions are both firewalled off prod ``:22``
(GOL-2282), so we cannot read ``/etc/grove/.env``. Route probing saturates at
grove_headless 1.45 (identical 20-route surface 1.45 -> 1.53), so it cannot tell
those apart either. But prod **HTTPS is reachable**, and Odoo will tell us the
installed module version directly:

    POST https://odoo.gatheringatthegrove.com/jsonrpc   (db `odoo`)
      common.authenticate                  -> uid
      object.execute_kw ir.module.module search_read
          [['name','=','grove_headless']] {'fields': ['installed_version', ...]}

That is the route GOL-2579 used to pin the live SHA to a single commit.

READ-ONLY BY CONSTRUCTION
-------------------------
``_execute_kw`` refuses any method outside ``ALLOWED_METHODS`` (``search_read``
only). There is no create/write/unlink code path in this file, and the
credential is never printed, echoed into ``$GITHUB_OUTPUT``, or included in any
error message.

WHAT A GREEN ACTUALLY MEANS  (read this before trusting it)
-----------------------------------------------------------
We compare **manifest versions**, not SHAs — prod does not expose its SHA. Many
commits share one manifest version, so:

  * RED  is a strong signal: the committed pin would move prod's grove_headless
         to a DIFFERENT version than it runs today. That is the rebuild-rollback
         footgun, caught.
  * GREEN is the weaker claim: "no version-level rollback". Same-version SHA
         drift (commits that did not bump ``__manifest__.py``) is invisible on
         this route and is NOT covered. Every one of the five historical
         incidents was a version-level move, so this covers the observed class.

Also note ``installed_version`` is the DB registry's record of the last module
*upgrade*, not a readout of the checked-out tree: if git-sync pulled new code
but the module was never upgraded, prod reports the pre-upgrade version.

EXIT CODES
----------
  0  match      committed pin's manifest version == prod installed_version
  1  DRIFT      they differ (the alert that matters)
  2  BROKEN     transient: network / HTTP / RPC transport failure -> retry once
  3  BROKEN     config: missing credential, unparseable HCL, auth refused,
                module absent -> never retry, a human must fix it

2 and 3 are both alerts. A watcher that cannot watch is not a green
(cf. GOL-2564, and the ``discord-status.sh --branch`` trap where ``|| true``
hid two alert paths for their entire lifetime).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Reuse the SAME block-scoped HCL parser reconcile-modules-pin.yml writes with,
# so the detector and the fixer can never disagree about which `default =` line
# is the modules pin (the GOL-1708 wrong-block trap).
from reconcile_modules_pin import (  # noqa: E402
    DEFAULT_TF_FILE,
    VAR_NAME,
    ReconcileError,
    _current_default,
    _find_block,
)

DEFAULT_PROD_URL = "https://odoo.gatheringatthegrove.com"
DEFAULT_DB = "odoo"
DEFAULT_MODULE = "grove_headless"
DEFAULT_MODULES_REPO = "Goldberry-Playground/grove-odoo-modules"

# Odoo series prefix. A manifest version written as "1.53.0" is stored by Odoo
# as "19.0.1.53.0"; one written as "19.0.1.53.0" is stored verbatim.
ODOO_SERIES = "19.0"

ALLOWED_METHODS = {"search_read"}

# Cloudflare sits in front of prod Odoo and 403s the default `Python-urllib/3.x`
# User-Agent outright (verified 2026-09-29: identical POST returns 403 with the
# stdlib default UA and 200 with any named one). Every JSON-RPC caller from CI
# or the agent plane MUST send an explicit UA or it gets a WAF 403 that looks
# exactly like an auth failure.
USER_AGENT = "grove-modules-pin-drift/1 (+GOL-2580; Goldberry-Playground/odoocker-goldberrygrove)"

EXIT_OK, EXIT_DRIFT, EXIT_BROKEN_TRANSIENT, EXIT_BROKEN_CONFIG = 0, 1, 2, 3


class TransientError(RuntimeError):
    """Network/transport problem — worth one retry."""


class ConfigError(RuntimeError):
    """Human-actionable problem — retrying changes nothing."""


# ── prod JSON-RPC (read-only) ────────────────────────────────────────────────


def _rpc(url: str, service: str, method: str, args: list, timeout: int) -> object:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
    ).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/jsonrpc",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # Almost certainly the Cloudflare WAF, not Odoo — see USER_AGENT.
            # Config, not transient: retrying an identical blocked request is pointless.
            raise ConfigError(
                f"prod JSON-RPC HTTP 403 on {service}.{method} — this is the Cloudflare WAF "
                f"rejecting the request, not an Odoo auth failure. Check the User-Agent."
            ) from None
        raise TransientError(f"prod JSON-RPC HTTP {exc.code} on {service}.{method}") from None
    except urllib.error.URLError as exc:
        raise TransientError(f"prod JSON-RPC unreachable: {exc.reason}") from None
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise TransientError(f"prod JSON-RPC bad/absent response: {type(exc).__name__}") from None

    if "error" in body:
        # Odoo folds auth refusal and model errors into the same envelope. Never
        # echo the payload back — it contains the credential we just sent.
        msg = (body["error"].get("data") or {}).get("message") or body["error"].get("message")
        raise ConfigError(f"prod JSON-RPC error on {service}.{method}: {msg}")
    return body.get("result")


def _execute_kw(url, db, uid, secret, model, method, args, kwargs, timeout):
    if method not in ALLOWED_METHODS:
        # Structural read-only guard: this script is wired into a nightly cron
        # holding a prod credential. Nothing here may ever mutate prod.
        raise ConfigError(f"refusing execute_kw method '{method}' — read-only by construction")
    return _rpc(url, "object", "execute_kw", [db, uid, secret, model, method, args, kwargs], timeout)


def prod_installed_version(url, db, login, secret, module, timeout=30) -> dict:
    uid = _rpc(url, "common", "authenticate", [db, login, secret, {}], timeout)
    if not uid:
        raise ConfigError(
            f"prod Odoo refused the credential for db '{db}' (authenticate returned falsy). "
            "Check the 1Password item and that the user is active."
        )
    rows = _execute_kw(
        url, db, uid, secret, "ir.module.module", "search_read",
        [[["name", "=", module]]],
        {"fields": ["name", "installed_version", "state", "write_date"]},
        timeout,
    )
    if not rows:
        raise ConfigError(f"module '{module}' not present in prod's ir.module.module registry")
    row = rows[0]
    if not row.get("installed_version"):
        raise ConfigError(
            f"module '{module}' has no installed_version (state={row.get('state')!r}) — not installed?"
        )
    return row


# ── committed pin -> manifest version ────────────────────────────────────────


def committed_ref(tf_file: str) -> str:
    try:
        with open(tf_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {tf_file}: {exc.strerror}") from None
    try:
        return _current_default(text[slice(*_find_block(text, VAR_NAME))])
    except ReconcileError as exc:
        raise ConfigError(f"cannot read var.{VAR_NAME} from {tf_file}: {exc}") from None


_MANIFEST_VERSION_RE = re.compile(
    r"""["']version["']\s*:\s*["']([0-9][0-9.]*)["']"""
)


def manifest_version_at(repo: str, sha: str, module: str, timeout=30, token: str = "") -> str:
    """Read <module>/__manifest__.py at `sha` from GitHub raw and pull `version`.

    grove-odoo-modules is public, so this needs no credential; a token is used
    only to lift the anonymous rate limit when one is available.
    """
    path = f"{module}/__manifest__.py"
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ConfigError(
                f"{path} not found at {repo}@{sha} — is the committed pin a real "
                "commit on that repo?"
            ) from None
        raise TransientError(f"GitHub raw HTTP {exc.code} for {repo}@{sha}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientError(f"GitHub raw unreachable: {exc}") from None

    m = _MANIFEST_VERSION_RE.search(body)
    if not m:
        raise ConfigError(f"no `version` key in {path} at {repo}@{sha}")
    return m.group(1)


def normalize(version: str) -> str:
    """Odoo prefixes the series onto short manifest versions. Compare like-for-like."""
    return version if version.count(".") >= 4 else f"{ODOO_SERIES}.{version}"


def _vtuple(version: str):
    try:
        return tuple(int(p) for p in normalize(version).split("."))
    except ValueError:
        return None



# ── best-effort: turn the live VERSION back into a candidate SHA set ─────────
#
# The DRIFT alert is only as useful as its fix line. "read the live SHA over
# ssh" means waking a human who has prod :22 access, which is exactly the
# round-trip that let GOL-2579 sit for six days. But grove-odoo-modules is
# public and its manifest version is monotonic, so the live version brackets
# the live SHA to the commits between one `__manifest__.py` bump and the next.
# In practice that is usually a single commit -- and then the alert can name
# the exact `reconcile-modules-pin.yml` argument and nobody has to ssh at all.
#
# STRICTLY BEST-EFFORT. Every failure here degrades to "could not resolve" and
# NEVER changes the exit code: this is an alert enrichment, not a check. A
# false alarm from a nice-to-have is how watchers get muted.

def _gh_json(url: str, timeout: int, token: str = ""):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def resolve_live_candidates(repo, module, live_version, timeout=30, token="", max_probe=25):
    """Return (candidate_shas, note). candidate_shas may be empty."""
    target = normalize(live_version)
    commits = _gh_json(
        f"https://api.github.com/repos/{repo}/commits"
        f"?path={module}/__manifest__.py&per_page={max_probe}",
        timeout, token)
    if not commits:
        return [], "no __manifest__.py history returned"

    # commits are newest-first. Find the newest bump whose version == target;
    # `newer` is the bump that superseded it (None if target is still HEAD's).
    idx = None
    for i, c in enumerate(commits):
        if normalize(manifest_version_at(repo, c["sha"], module, timeout, token)) == target:
            idx = i
            break
    if idx is None:
        return [], (f"prod version {target} does not match any of the last "
                    f"{len(commits)} __manifest__.py bumps")

    bump = commits[idx]["sha"]
    newer = commits[idx - 1]["sha"] if idx > 0 else None
    if newer is None:
        # Target is the current manifest version: every commit from `bump` to
        # main HEAD still reports it, so the set is open-ended. Name the bump
        # as the floor rather than pretending to be exact.
        return [bump], (f"{target} is the CURRENT manifest version — {bump[:12]} is the "
                        "floor, later same-version commits are indistinguishable")

    cmp_ = _gh_json(f"https://api.github.com/repos/{repo}/compare/{bump}...{newer}", timeout, token)
    # commits reachable from `newer` but not `bump`, minus `newer` itself:
    # those plus `bump` all still carry `target`.
    between = [c["sha"] for c in cmp_.get("commits", []) if c["sha"] != newer]
    candidates = [bump] + between
    if len(candidates) == 1:
        return candidates, ""

    # Several commits share the version, but they are only genuinely ambiguous
    # if they differ in code Odoo LOADS. grove-odoo-modules also carries
    # scripts/, docs/ and CI at the repo root; a candidate that changes nothing
    # under `<module>/` is byte-identical to `bump` as far as the running
    # registry is concerned, so reconciling onto `bump` is not a guess. This is
    # a soundness argument, not a heuristic -- it is what makes the alert
    # actionable without prod ssh.
    tail = candidates[-1]
    span = _gh_json(f"https://api.github.com/repos/{repo}/compare/{bump}...{tail}", timeout, token)
    touched = [f["filename"] for f in span.get("files", [])
               if f["filename"].startswith(module + "/")]
    if not touched:
        return [bump], (f"{len(candidates)} commits carry {target}, but none after {bump[:12]} "
                        f"touch {module}/ — they are runtime-identical, so {bump[:12]} is the "
                        "correct reconcile target regardless of which one prod checked out")
    return candidates, ""


# ── report ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Detect prod modules-pin drift (GOL-2580).")
    ap.add_argument("--file", default=DEFAULT_TF_FILE, help="prod variables.tf")
    ap.add_argument("--module", default=DEFAULT_MODULE)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="also emit a machine-readable line")
    args = ap.parse_args(argv)

    url = os.environ.get("PROD_ODOO_URL") or DEFAULT_PROD_URL
    db = os.environ.get("PROD_ODOO_DB") or DEFAULT_DB
    login = os.environ.get("PROD_ODOO_LOGIN") or ""
    secret = os.environ.get("PROD_ODOO_SECRET") or ""
    repo = os.environ.get("MODULES_REPO") or DEFAULT_MODULES_REPO
    gh_token = os.environ.get("GH_RAW_TOKEN") or ""

    if not login or not secret:
        # Fail CLOSED and say exactly what is missing. A watcher that goes green
        # because its credential vanished is the failure mode this issue exists
        # to kill.
        print(
            "note: PROD_ODOO_LOGIN / PROD_ODOO_SECRET are empty — the drift watcher "
            "cannot read prod and is therefore BLIND, not clean.",
            file=sys.stderr,
        )
        print(
            "note: provision the read-only prod Odoo credential in 1Password "
            "(see .github/workflows/prod-modules-pin-drift.yml header).",
            file=sys.stderr,
        )
        return EXIT_BROKEN_CONFIG

    try:
        ref = committed_ref(args.file)
        committed_version = manifest_version_at(repo, ref, args.module, args.timeout, gh_token)
        row = prod_installed_version(url, db, login, secret, args.module, args.timeout)
    except ConfigError as exc:
        print(f"note: {exc}", file=sys.stderr)
        return EXIT_BROKEN_CONFIG
    except TransientError as exc:
        print(f"note: {exc}", file=sys.stderr)
        return EXIT_BROKEN_TRANSIENT

    live_version = row["installed_version"]
    committed_n, live_n = normalize(committed_version), normalize(live_version)

    print(f"module:    {args.module}")
    print(f"committed: {ref} -> {committed_n}   (var.{VAR_NAME} in {args.file})")
    print(f"prod-live: {live_n}   (ir.module.module, last write {row.get('write_date')})")

    if committed_n == live_n:
        print("result:    MATCH — a droplet rebuild would not move grove_headless's version.")
        print(
            "note:      green means 'no version-level rollback'. Commits that do not bump "
            "__manifest__.py are invisible on this route (prod exposes no SHA)."
        )
        if args.json:
            print("json: " + json.dumps(
                {"result": "match", "committed_ref": ref,
                 "committed_version": committed_n, "live_version": live_n}))
        return EXIT_OK

    cv, lv = _vtuple(committed_version), _vtuple(live_version)
    if cv is not None and lv is not None and cv < lv:
        direction = (
            f"DRIFT — committed pin is BEHIND prod. A droplet REBUILD would roll "
            f"grove_headless BACKWARD {live_n} -> {committed_n}."
        )
    elif cv is not None and lv is not None and cv > lv:
        direction = (
            f"DRIFT — committed pin is AHEAD of prod ({live_n} -> {committed_n}). A rebuild "
            f"would ship un-promoted commits; prod was likely never upgraded to the committed pin."
        )
    else:
        direction = f"DRIFT — committed {committed_n} != prod-live {live_n} (versions not comparable)."

    print(f"DRIFT:     {direction}")

    candidates, why = [], ""
    try:
        candidates, why = resolve_live_candidates(repo, args.module, live_version,
                                                  args.timeout, gh_token)
    except Exception as exc:  # noqa: BLE001 - enrichment only, never fatal
        why = f"{type(exc).__name__}: {exc}"

    if len(candidates) == 1:
        print(
            f"DRIFT:     fix = gh workflow run reconcile-modules-pin.yml "
            f"-f modules_sha={candidates[0]}"
        )
        print(
            f"DRIFT:     ({why or f'{candidates[0][:12]} is the ONLY grove-odoo-modules commit carrying {live_n}'}"
            "; no prod ssh needed to confirm it.)"
        )
    elif candidates:
        print(
            "DRIFT:     fix = gh workflow run reconcile-modules-pin.yml -f modules_sha=<sha>, "
            f"where <sha> is one of the {len(candidates)} commits carrying {live_n}:"
        )
        for sha in candidates[:8]:
            print(f"DRIFT:       {sha}")
        if len(candidates) > 8:
            print(f"DRIFT:       ... and {len(candidates) - 8} more")
        print(
            "DRIFT:     disambiguate with `grep CUSTOM_MODULES_REF /etc/grove/.env` on prod-odoo."
        )
    else:
        print(
            "DRIFT:     fix = read the live SHA "
            "(`grep CUSTOM_MODULES_REF /etc/grove/.env` on prod-odoo), then run: "
            "gh workflow run reconcile-modules-pin.yml -f modules_sha=<sha>"
        )
        if why:
            print(f"note:      could not narrow the live SHA from the version ({why}).")
    print(
        "DRIFT:     until reconciled, NEVER rebuild prod-odoo without "
        "`-var custom_modules_ref=<live SHA>`."
    )
    if args.json:
        print("json: " + json.dumps(
            {"result": "drift", "committed_ref": ref,
             "committed_version": committed_n, "live_version": live_n,
             "live_sha_candidates": candidates}))
    return EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
