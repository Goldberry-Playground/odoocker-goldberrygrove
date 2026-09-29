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

SECOND, CONFIG-INDEPENDENT LEG -- `--census` (GOL-2576). The per-env check
above can only assert things the repo declares, so an UN-CODIFIED droplet is
invisible to it by construction: no resource, no expectation, no finding. Two
of the account's five droplets were in exactly that state on 2026-09-29
(`ghostgoldberrygrove-nyc1` GOL-2566, `agenticos-droplet` GOL-2569) and both
were found by a HAND-RUN audit -- nothing scheduled would have found either,
or the next one. The census asks the account itself instead of the repo:

  * every live droplet must be covered by SOME firewall, counting both
    explicit `droplet_ids` and TAG-resolved membership;
  * no firewall may allow :22 from the whole internet -- attached or not, a
    dormant world-open rule is a lockless door waiting for a droplet.

Anything else is a finding unless it is on a dated, issue-referencing entry in
`infra/terraform/firewall-census-allowlist.json`, so an accepted exposure is a
reviewed commit rather than silence. An empty allowlist is the goal state.

Usage:
  infra/terraform/scripts/check-firewall-membership.py production
  infra/terraform/scripts/check-firewall-membership.py production observability
  infra/terraform/scripts/check-firewall-membership.py --allow-absent qa-app-platform
  infra/terraform/scripts/check-firewall-membership.py --census

Env required:
  DO_TOKEN | DIGITALOCEAN_TOKEN | TF_VAR_do_token   read-only is enough

Exit codes:
  0  every codified firewall contains exactly the droplets its config names,
     and (with --census) every live droplet is covered and no firewall opens
     :22 to the world
  1  membership drift, or a census finding
  2  bad env: no token, no such env dir, bad allowlist, or DO unreachable
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ENVS = Path(__file__).resolve().parents[1] / "environments"
ALLOWLIST = Path(__file__).resolve().parents[1] / "firewall-census-allowlist.json"
API = "https://api.digitalocean.com/v2"

# "Open to the world" on either stack. DO stores an empty source as an absent
# key, not as 0.0.0.0/0, so only these literals mean "everyone".
WORLD = {"0.0.0.0/0", "::/0"}
ISSUE_REF = re.compile(r"^GOL-\d+$")

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


def ports_cover_22(ports) -> bool:
    """Does a DO rule's `ports` value include 22?

    DO renders "every port" as the string "0" (and the UI as "all"), a single
    port as "22", and a range as "20-30". Matching only the literal "22" would
    wave through the strictly WORSE `ports: "0"` -- all ports from anywhere --
    so all three forms are decoded here.
    """
    if ports is None:
        # No `ports` key at all is how DO renders protocol icmp, which carries
        # no port. Nothing to do with SSH.
        return False
    ports = str(ports).strip()
    if ports in ("0", "all", ""):
        return True
    if "-" in ports:
        lo, _, hi = ports.partition("-")
        try:
            return int(lo) <= 22 <= int(hi)
        except ValueError:
            # Unparseable is not provably safe. Say so rather than pass.
            return True
    return ports == "22"


def load_allowlist():
    """-> (droplet_exempt, firewall_exempt, problems).

    Entries are keyed by DO id and MUST carry an `issue` (GOL-NNNN) and a
    dated `expires`. A malformed or expired entry does not suppress anything
    and is itself reported -- an exemption that outlives its review is how a
    "temporary" exposure becomes permanent.
    """
    problems: list[str] = []
    if not ALLOWLIST.exists():
        return {}, {}, problems
    try:
        raw = json.loads(ALLOWLIST.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return None, None, [f"allowlist {ALLOWLIST.name} is unreadable: {e}"]

    today = dt.date.today()
    out: dict[str, dict] = {"uncovered_droplets": {}, "open_ssh_firewalls": {}}
    for section in out:
        for entry in raw.get(section, []) or []:
            key = entry.get("id")
            issue = str(entry.get("issue", ""))
            expires = str(entry.get("expires", ""))
            if key in (None, ""):
                problems.append(f"{section}: entry with no `id`: {entry!r}")
                continue
            if not ISSUE_REF.match(issue):
                problems.append(f"{section}[{key}]: `issue` must be GOL-NNNN, got {issue!r}")
                continue
            try:
                when = dt.date.fromisoformat(expires)
            except ValueError:
                problems.append(f"{section}[{key}]: `expires` must be YYYY-MM-DD, got {expires!r}")
                continue
            if when < today:
                problems.append(
                    f"{section}[{key}] ({entry.get('name', '?')}): exemption EXPIRED {expires} "
                    f"-- re-review {issue} or fix the exposure"
                )
                continue
            out[section][str(key)] = entry
    return out["uncovered_droplets"], out["open_ssh_firewalls"], problems


def census(live_droplets, live_fws) -> bool:
    """Account-wide, config-independent. -> True if anything was found.

    Deliberately asks the ACCOUNT, not the repo. The per-env check compares
    live membership against Terraform, so it can only ever see droplets
    Terraform declares; this one enumerates what actually exists and demands
    that each box be behind something.
    """
    droplet_exempt, fw_exempt, problems = load_allowlist()
    if droplet_exempt is None:
        for msg in problems:
            print(f"  ! census: {msg}")
        return True

    print(f"\n=== census: {len(live_droplets)} live droplet(s), {len(live_fws)} live firewall(s)")
    found = False
    for msg in problems:
        print(f"  ALLOWLIST {msg}")
        found = True

    # Coverage. A firewall attaches EITHER by explicit droplet_ids OR by tag,
    # and the API does not fold tag membership into droplet_ids -- so a
    # tags-only firewall would look like it protects nothing if we read
    # droplet_ids alone. Resolve tags against each droplet's own tag list.
    covered: dict[int, list[str]] = {}
    fw_tags: list[tuple[str, set[str]]] = [
        (f["name"], set(f.get("tags") or [])) for f in live_fws
    ]
    for f in live_fws:
        for did in f.get("droplet_ids") or []:
            covered.setdefault(did, []).append(f["name"])
    for d in live_droplets:
        dtags = set(d.get("tags") or [])
        for fname, tags in fw_tags:
            if tags and dtags & tags:
                covered.setdefault(d["id"], []).append(f"{fname} (via tag)")

    for d in sorted(live_droplets, key=lambda x: x["name"]):
        did, name = d["id"], d["name"]
        by = covered.get(did)
        if by:
            print(f"  OK droplet {name} ({did}): behind {', '.join(sorted(set(by)))}")
            continue
        exempt = droplet_exempt.get(str(did))
        if exempt:
            print(
                f"  ALLOWED droplet {name} ({did}): in NO firewall, exempt until "
                f"{exempt['expires']} per {exempt['issue']}"
            )
            if exempt.get("name") and exempt["name"] != name:
                print(f"  ! allowlist entry for {did} says name {exempt['name']!r}, live name is {name!r}")
            continue
        print(
            f"  UNCOVERED droplet {name} (id {did}, created {d.get('created_at', '?')}, "
            f"region {d.get('region', {}).get('slug', '?')}): in NO cloud firewall"
        )
        found = True

    # The reverse hazard. A firewall with :22 <- 0.0.0.0/0 is harmless only for
    # as long as nothing is attached to it; the moment a droplet joins -- or is
    # auto-adopted by a tag -- it is world-open SSH that no review asked for.
    # Checked whether or not it currently holds a droplet, for that reason.
    for f in sorted(live_fws, key=lambda x: x["name"]):
        for rule in f.get("inbound_rules") or []:
            if rule.get("protocol") not in ("tcp", "all"):
                continue
            if not ports_cover_22(rule.get("ports")):
                continue
            addrs = set((rule.get("sources") or {}).get("addresses") or [])
            world = addrs & WORLD
            if not world:
                continue
            exempt = fw_exempt.get(str(f["id"]))
            attached = len(f.get("droplet_ids") or []) + len(f.get("tags") or [])
            where = f"{len(f.get('droplet_ids') or [])} droplet(s), {len(f.get('tags') or [])} tag(s)"
            if exempt:
                print(
                    f"  ALLOWED firewall {f['name']} ({f['id']}): :22 <- {sorted(world)}, "
                    f"exempt until {exempt['expires']} per {exempt['issue']}"
                )
                break
            print(
                f"  OPEN-SSH firewall {f['name']} (id {f['id']}, {where}): "
                f"ports {rule.get('ports')!r} <- {sorted(world)}"
                + ("" if attached else " -- dormant today, world-open the moment anything attaches")
            )
            found = True
            break

    if not found:
        print("  OK: every live droplet is behind a firewall, and none opens :22 to the world")
    return found


# --------------------------------------------------------------------------
# Offline self-test (GOL-2576). No token, no network: the census is mostly
# decode-and-compare, and the two things most likely to be got wrong -- DO's
# "0 means every port" encoding and TAG-resolved membership -- are exactly the
# things a live run against this account does not exercise, because no firewall
# here attaches by tag and none opens a port range. So they are pinned here.
# --------------------------------------------------------------------------


def _fw(name, *, fid="fw-0", droplet_ids=(), tags=(), rules=()):
    return {
        "id": fid,
        "name": name,
        "droplet_ids": list(droplet_ids),
        "tags": list(tags),
        "inbound_rules": list(rules),
        "status": "succeeded",
    }


def _rule(protocol="tcp", ports="22", addresses=("0.0.0.0/0",)):
    return {"protocol": protocol, "ports": ports, "sources": {"addresses": list(addresses)}}


def _droplet(did, name, tags=()):
    return {
        "id": did,
        "name": name,
        "tags": list(tags),
        "created_at": "2026-01-01T00:00:00Z",
        "region": {"slug": "nyc1"},
    }


def selftest() -> int:
    global ALLOWLIST
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    # -- ports decoding. "0" is DO's "every port", which is strictly WORSE than
    # 22 and would sail past a literal == "22" match.
    for ports, want in [
        ("22", True),
        ("0", True),
        ("all", True),
        ("", True),
        ("20-30", True),
        ("1-65535", True),
        (22, True),
        ("80", False),
        ("2222", False),
        ("80-90", False),
        ("23-65535", False),
        (None, False),
        ("not-a-port", True),  # unparseable is not provably safe
    ]:
        check(f"ports_cover_22({ports!r})", ports_cover_22(ports), want)

    def run(droplets, fws, allowlist=None):
        """-> (found, printed output) with ALLOWLIST pointed at `allowlist`."""
        global ALLOWLIST
        saved = ALLOWLIST
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            if allowlist is not None:
                path.write_text(json.dumps(allowlist))
            ALLOWLIST = path
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    found = census(droplets, fws)
            finally:
                ALLOWLIST = saved
        return found, buf.getvalue()

    today = dt.date.today()
    future = (today + dt.timedelta(days=30)).isoformat()
    past = (today - dt.timedelta(days=1)).isoformat()

    # -- covered by explicit droplet_ids
    found, out = run([_droplet(1, "a")], [_fw("f", droplet_ids=[1])])
    check("explicit membership is covered", found, False)

    # -- covered by TAG. The API does not fold tag membership into
    # droplet_ids, so reading droplet_ids alone would call this uncovered.
    found, out = run([_droplet(1, "a", tags=["prod"])], [_fw("f", tags=["prod"])])
    check("tag membership is covered", found, False)
    check("tag membership is labelled", "via tag" in out, True)

    # -- a tagless firewall must not adopt a tagless droplet
    found, out = run([_droplet(1, "a")], [_fw("f")])
    check("tagless fw does not cover tagless droplet", found, True)
    check("uncovered droplet is named", "UNCOVERED droplet a (id 1" in out, True)
    check("uncovered droplet reports created_at", "created 2026-01-01T00:00:00Z" in out, True)

    # -- a droplet's OTHER tags must not match
    found, _ = run([_droplet(1, "a", tags=["dev"])], [_fw("f", tags=["prod"])])
    check("non-matching tag does not cover", found, True)

    # -- open-SSH detection, attached or not
    found, out = run([], [_fw("open", rules=[_rule()])])
    check(":22 from 0.0.0.0/0 is a finding", found, True)
    check("open-ssh names the firewall", "OPEN-SSH firewall open" in out, True)
    check("dormant is called out", "dormant today" in out, True)

    found, _ = run([], [_fw("v6", rules=[_rule(addresses=["::/0"])])])
    check(":22 from ::/0 is a finding", found, True)

    found, _ = run([], [_fw("allports", rules=[_rule(ports="0")])])
    check("all-ports from the world is a finding", found, True)

    found, _ = run([], [_fw("narrow", rules=[_rule(addresses=["1.2.3.4/32"])])])
    check(":22 from a /32 is fine", found, False)

    found, _ = run([], [_fw("udp", rules=[_rule(protocol="udp")])])
    check("udp/22 from the world is not SSH", found, False)

    found, _ = run([], [_fw("http", rules=[_rule(ports="443")])])
    check(":443 from the world is not this check's business", found, False)

    # -- allowlist: a dated, issue-referencing entry suppresses
    found, out = run(
        [_droplet(1, "a")],
        [],
        {"uncovered_droplets": [{"id": 1, "name": "a", "issue": "GOL-1", "expires": future}]},
    )
    check("valid exemption suppresses", found, False)
    check("exemption is still printed", "ALLOWED droplet a (1)" in out, True)

    found, out = run(
        [], [_fw("open", fid="u-1", rules=[_rule()])],
        {"open_ssh_firewalls": [{"id": "u-1", "name": "open", "issue": "GOL-1", "expires": future}]},
    )
    check("valid fw exemption suppresses", found, False)

    # -- an exemption that outlives its expiry stops suppressing AND reports
    found, out = run(
        [_droplet(1, "a")],
        [],
        {"uncovered_droplets": [{"id": 1, "name": "a", "issue": "GOL-1", "expires": past}]},
    )
    check("expired exemption does not suppress", found, True)
    check("expired exemption is reported", "EXPIRED" in out, True)
    check("expired exemption still surfaces the droplet", "UNCOVERED droplet a" in out, True)

    # -- no issue ref, no exemption
    found, out = run(
        [_droplet(1, "a")], [], {"uncovered_droplets": [{"id": 1, "name": "a", "expires": future}]}
    )
    check("issue-less exemption does not suppress", found, True)
    check("issue-less exemption is reported", "must be GOL-NNNN" in out, True)

    # -- a garbled date is not a free pass either
    found, out = run(
        [_droplet(1, "a")],
        [],
        {"uncovered_droplets": [{"id": 1, "name": "a", "issue": "GOL-1", "expires": "soon"}]},
    )
    check("undated exemption does not suppress", found, True)

    # -- a renamed droplet keeps its exemption but says so
    found, out = run(
        [_droplet(1, "renamed")],
        [],
        {"uncovered_droplets": [{"id": 1, "name": "a", "issue": "GOL-1", "expires": future}]},
    )
    check("rename does not break the exemption", found, False)
    check("rename is flagged", "live name is 'renamed'" in out, True)

    # -- an unreadable allowlist fails closed rather than exempting nothing quietly
    saved = ALLOWLIST
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "allow.json"
        bad.write_text("{ not json")
        ALLOWLIST = bad
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            found = census([], [])
        ALLOWLIST = saved
    check("unreadable allowlist is a finding", found, True)

    # -- the real allowlist in this repo must itself be valid
    dex, fex, problems = load_allowlist()
    check(f"repo allowlist parses ({ALLOWLIST.name})", dex is not None, True)
    check(f"repo allowlist has no problems: {problems}", problems, [])

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        print(f"\n{len(failures)} self-test failure(s)", file=sys.stderr)
        return 1
    print("selftest: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "envs",
        nargs="*",
        help="env dir name(s) under infra/terraform/environments (optional with --census)",
    )
    ap.add_argument(
        "--census",
        action="store_true",
        help="ALSO run the account-wide census: every live droplet must be behind some "
        "firewall, and no firewall may open :22 to 0.0.0.0/0 or ::/0. Reads the DO "
        "account rather than the repo, so it sees UN-CODIFIED droplets the per-env "
        "check cannot (GOL-2576).",
    )
    ap.add_argument(
        "--allow-absent",
        action="store_true",
        help="a declared droplet OR firewall that does not exist live is a SKIP, "
        "not drift. For ephemeral/tearable envs (preview, qa-app-platform between "
        "trains) ONLY -- never for production, where an absent droplet or a "
        "vanished firewall is its own alarm.",
    )
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="run the offline logic tests and exit -- no token, no network",
    )
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.envs and not args.census:
        ap.error("give at least one env, or --census, or both")

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

    census_found = census(live_droplets, live_fws) if args.census else False

    if not drift and not census_found:
        if args.envs:
            print("\nOK: every codified firewall contains the droplets its config names")
        return 0

    # Flush so the per-firewall report lands ahead of this banner when stdout
    # and stderr share a terminal or a CI log.
    sys.stdout.flush()
    if census_found:
        print(
            "\nCensus finding. An UNCOVERED droplet has no code behind it by definition --\n"
            "the fix is to codify a firewall for it and apply, not to click one on:\n"
            "  infra/terraform/environments/production/<name>-fw.tf\n"
            "An OPEN-SSH firewall should be deleted if it is dormant, or have its :22\n"
            "rule narrowed to known /32s if it is in use. If an exposure is genuinely\n"
            "accepted, add a dated, GOL-referencing entry to\n"
            f"  {ALLOWLIST.relative_to(Path.cwd()) if ALLOWLIST.is_relative_to(Path.cwd()) else ALLOWLIST}\n"
            "so the acceptance is a reviewed commit with an expiry, not silence.",
            file=sys.stderr,
        )
    if not drift:
        return 1
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
