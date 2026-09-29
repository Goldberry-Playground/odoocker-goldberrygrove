#!/usr/bin/env python3
"""Assert every codified DO cloud firewall actually CONTAINS the droplets it names.

Why this exists (GOL-2565). On 2026-09-16 `grove-prod-odoo` was replaced (a
`user_data` edit forces a droplet REPLACE) and `grove-prod-odoo-fw` was never
re-converged onto the new droplet id. For 13 days the firewall existed, its
*rules* were correct, and its `droplet_ids` was `[]` -- so nothing at the DO
edge filtered production Odoo and `:22` answered the whole internet. Same
failure shape as the `grove-obs-fw` drift in ADR-010.

This is deliberately COMPLEMENTARY to
`environments/observability/scripts/check-firewall.sh` (GOL-2333), which
compares per-port source unions -- "who may reach 5080?". That script would
have passed grove-prod-odoo-fw with flying colours: the rules were right. It
only *prints* `droplet_ids` as a note. Membership -- "is the box actually
behind the firewall at all?" -- is the question nothing asserted, and it is
the question this script answers, for every env, from code.

Reads the env's `*.tf` directly and the DO API read-only. No Terraform, no S3
backend, no state lock, no tfvars -- so it is safe to run on a cron, from the
agent plane, or as a pre-/post-apply proof, concurrently with anything else.

Usage:
  infra/terraform/scripts/check-firewall-membership.py production
  infra/terraform/scripts/check-firewall-membership.py production observability
  infra/terraform/scripts/check-firewall-membership.py --allow-absent qa-app-platform

Env required:
  DO_TOKEN | DIGITALOCEAN_TOKEN | TF_VAR_do_token   read-only is enough

Exit codes:
  0  every codified firewall contains exactly the droplets its config names
  1  membership drift (missing, unexpected, ambiguous, or firewall absent)
  2  bad env: no token, no such env dir, or DO unreachable
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ENVS = Path(__file__).resolve().parents[1] / "environments"
API = "https://api.digitalocean.com/v2"

# `name = "literal"` with no ${...} interpolation. Anything interpolated (the
# preview env's "${local.name}-fw", a per-PR ephemeral) is not statically
# resolvable, so we SKIP it rather than guess -- a wrong guess here would
# either false-alarm nightly or, worse, quietly "pass" the wrong firewall.
LITERAL_NAME = re.compile(r'^\s*name\s*=\s*"([^"$]*)"\s*$', re.M)
DROPLET_REF = re.compile(r"digitalocean_droplet\.([A-Za-z0-9_-]+)\.id")
MODULE_REF = re.compile(r"module\.([A-Za-z0-9_-]+)\.droplet_id")
DROPLET_IDS = re.compile(r"droplet_ids\s*=\s*\[(.*?)\]", re.S)


def iter_blocks(text: str):
    """Yield (header, body) for each TOP-LEVEL HCL block.

    Brace-depth scan rather than a regex over the whole file: nested blocks
    (`inbound_rule { ... }`) and braces inside strings would otherwise let a
    body run past its closing brace and swallow the next resource.
    """
    i, n = 0, len(text)
    while i < n:
        brace = text.find("{", i)
        if brace == -1:
            return
        line_start = text.rfind("\n", 0, brace) + 1
        header = text[line_start:brace].strip()
        depth, j, in_str = 0, brace, False
        while j < n:
            c = text[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= n:
            return
        yield header, text[brace + 1 : j]
        i = j + 1


def literal_name(body: str) -> str | None:
    # First top-level `name = "..."`. Sub-blocks get scanned too, but every
    # resource we care about declares its own name before any sub-block, and
    # a firewall's inbound_rule/outbound_rule carry no `name` at all.
    m = LITERAL_NAME.search(body)
    return m.group(1) if m else None


def parse_env(env_dir: Path):
    """-> (droplets, firewalls). Keys are Terraform addresses."""
    droplets: dict[str, str | None] = {}
    firewalls: dict[str, dict] = {}
    for tf in sorted(env_dir.glob("*.tf")):
        text = tf.read_text()
        for header, body in iter_blocks(text):
            rm = re.match(r'resource\s+"([^"]+)"\s+"([^"]+)"', header)
            mm = re.match(r'module\s+"([^"]+)"', header)
            if rm and rm.group(1) == "digitalocean_droplet":
                droplets[f"digitalocean_droplet.{rm.group(2)}"] = literal_name(body)
            elif mm:
                # A module block is only interesting if some firewall
                # references its droplet_id; recorded unconditionally, cheap.
                droplets[f"module.{mm.group(1)}"] = literal_name(body)
            elif rm and rm.group(1) == "digitalocean_firewall":
                ids = DROPLET_IDS.search(body)
                refs = []
                if ids:
                    refs = [f"digitalocean_droplet.{m}" for m in DROPLET_REF.findall(ids.group(1))]
                    refs += [f"module.{m}" for m in MODULE_REF.findall(ids.group(1))]
                firewalls[f"digitalocean_firewall.{rm.group(2)}"] = {
                    "fw_name": literal_name(body),
                    "refs": refs,
                    "file": tf.name,
                }
    return droplets, firewalls


def api(path: str, token: str):
    req = urllib.request.Request(
        f"{API}/{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("envs", nargs="+", help="env dir name(s) under infra/terraform/environments")
    ap.add_argument(
        "--allow-absent",
        action="store_true",
        help="a declared droplet OR firewall that does not exist live is a SKIP, "
        "not drift. For ephemeral/tearable envs (preview, qa-app-platform between "
        "trains) ONLY -- never for production, where an absent droplet or a "
        "vanished firewall is its own alarm.",
    )
    args = ap.parse_args()

    token = (
        os.environ.get("DO_TOKEN")
        or os.environ.get("DIGITALOCEAN_TOKEN")
        or os.environ.get("TF_VAR_do_token")
    )
    if not token:
        print(
            "::error::No DO API token in env (checked DO_TOKEN, DIGITALOCEAN_TOKEN, TF_VAR_do_token)",
            file=sys.stderr,
        )
        return 2

    try:
        live_fws = api("firewalls?per_page=200", token)["firewalls"]
        live_droplets = api("droplets?per_page=200", token)["droplets"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        print(f"::error::DO API unreachable or unexpected response: {e}", file=sys.stderr)
        return 2

    fw_by_name = {f["name"]: f for f in live_fws}
    ids_by_droplet_name: dict[str, list[int]] = {}
    for d in live_droplets:
        ids_by_droplet_name.setdefault(d["name"], []).append(d["id"])

    drift = False
    for env in args.envs:
        env_dir = REPO_ENVS / env
        if not env_dir.is_dir():
            print(f"::error::no such env dir: {env_dir}", file=sys.stderr)
            return 2

        droplets, firewalls = parse_env(env_dir)
        print(f"\n=== env {env}: {len(firewalls)} codified firewall(s)")
        if not firewalls:
            print("  (none declared -- nothing to assert)")
            continue

        for addr, fw in sorted(firewalls.items()):
            name, refs = fw["fw_name"], fw["refs"]
            if not name:
                print(f"  SKIP {addr} ({fw['file']}): firewall name is interpolated, not statically resolvable")
                continue
            if not refs:
                print(f"  SKIP {addr} -> {name}: declares no droplet_ids")
                continue

            live = fw_by_name.get(name)
            if live is None:
                # A torn-down env takes its firewall with it, so under
                # --allow-absent "no such firewall" is the expected steady
                # state, not drift. For production it is the loudest possible
                # signal: the rules themselves are gone.
                if args.allow_absent:
                    print(f"  SKIP {name}: no such firewall live (env torn down?)")
                else:
                    print(f"  DRIFT {name}: declared in {fw['file']} but NO such firewall exists live")
                    drift = True
                continue

            live_ids = set(live["droplet_ids"])
            status = live.get("status")
            if status != "succeeded":
                print(f"  ! {name}: status='{status}', not 'succeeded' -- a prior apply may be incomplete")

            expected: set[int] = set()
            unresolved = False
            for ref in refs:
                dname = droplets.get(ref)
                if not dname:
                    print(f"  SKIP {name}: droplet {ref} has an interpolated/absent name in code")
                    unresolved = True
                    continue
                matches = ids_by_droplet_name.get(dname, [])
                if not matches:
                    if args.allow_absent:
                        print(f"  SKIP {name}: droplet '{dname}' does not exist live (env torn down?)")
                        unresolved = True
                    else:
                        print(f"  DRIFT {name}: droplet '{dname}' ({ref}) does not exist live")
                        drift = True
                    continue
                if len(matches) > 1:
                    # Two boxes answering to one name means a replace left an
                    # orphan. Which one the firewall "should" hold is not ours
                    # to guess -- fail and make a human look.
                    print(f"  DRIFT {name}: droplet name '{dname}' is AMBIGUOUS live: ids {sorted(matches)}")
                    drift = True
                    unresolved = True
                    continue
                expected.add(matches[0])

            if unresolved and not expected:
                continue

            missing = expected - live_ids
            unexpected = live_ids - expected
            if missing:
                print(
                    f"  DRIFT {name}: droplet_ids MISSING {sorted(missing)} "
                    f"(live={sorted(live_ids)}) -- these droplets are NOT behind this firewall"
                )
                drift = True
            if unexpected and not unresolved:
                # An undeclared droplet inside a prod firewall inherits its
                # allow rules with no code behind it -- no review ever saw it.
                print(
                    f"  DRIFT {name}: droplet_ids UNEXPECTED {sorted(unexpected)} "
                    f"(not declared in {fw['file']})"
                )
                drift = True
            if not missing and not unexpected:
                print(f"  OK {name}: droplet_ids={sorted(live_ids)} matches {fw['file']}")

    if not drift:
        print("\nOK: every codified firewall contains the droplets its config names")
        return 0

    # Flush so the per-firewall report lands ahead of this banner when stdout
    # and stderr share a terminal or a CI log.
    sys.stdout.flush()
    print(
        "\nMembership drift. `droplet_ids` is updatable IN PLACE -- reconciling it is\n"
        "not a droplet replace. Reconcile from code, never by hand in the DO UI:\n"
        "  terraform -chdir=infra/terraform/environments/<env> plan  -target=digitalocean_firewall.<label>\n"
        "  terraform -chdir=infra/terraform/environments/<env> apply -target=digitalocean_firewall.<label>\n"
        "Confirm the plan is ONLY the in-place firewall update and shows no droplet\n"
        "replace before applying (GOL-817: never bare-apply production). Converging\n"
        "an allowlist is also a lockout risk -- verify your own SSH still works\n"
        "afterwards (GOL-1842).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
