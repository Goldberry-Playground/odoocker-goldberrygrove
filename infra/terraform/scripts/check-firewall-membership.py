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

Resolves three ways a firewall can name its droplets: a managed
`digitalocean_droplet.<x>.id`, a `module.<x>.droplet_id`, and a
`data.digitalocean_droplet.<x>[0].id` -- the last is how the holding-action
fences for the un-codified snowflakes (GOL-2566, GOL-2569) reach boxes
Terraform does not manage, and it is resolved by droplet NAME (through a
`variable` default when the name is `var.x`). `count`-gated firewalls whose
gate is off are reported as an expected SKIP rather than drift, because
merging those files deliberately is not an apply -- but the moment such a
firewall exists live, its membership is asserted like any other.

SECOND, CONFIG-INDEPENDENT LEG -- `--census` (GOL-2576). The per-env check
above can only assert things the repo declares, so an UN-CODIFIED droplet is
invisible to it by construction: no resource, no expectation, no finding. Two
of the account's five droplets were in exactly that state on 2026-09-29
(`ghostgoldberrygrove-nyc1` GOL-2566, `agenticos-droplet` GOL-2569) and both
were found by a HAND-RUN audit -- nothing scheduled would have found either,
or the next one. The census asks the account itself instead of the repo:

  * every live droplet must be covered by SOME firewall, counting both
    explicit `droplet_ids` and TAG-resolved membership;
  * no firewall may allow a SENSITIVE PORT from the whole internet -- attached
    or not, a dormant world-open rule is a lockless door waiting for a droplet;
  * every MANAGED DATABASE cluster must have trusted sources that actually
    narrow something (GOL-2582).

THE THIRD INVISIBLE SURFACE -- managed databases (GOL-2582). A cluster's
trusted sources are a DIFFERENT API object (`/v2/databases/{id}/firewall`) and
a different Terraform resource (`digitalocean_database_firewall`) from the
droplet firewalls under `/v2/firewalls`. Neither leg above can ever see them,
however much droplet coverage it gains -- green because the surface is
invisible, not because the surface is safe, exactly like an un-codified
firewall. And the trap is the EMPTY LIST: on DO `trusted_sources: []` is not
"closed", it means the cluster's public host answers :25060 for ANY source that
presents credentials, so the whole perimeter is the doadmin/odoo password. A
check that asks only "does the firewall object exist?" reads that as healthy.
`grove-qa-l3-pg` was in exactly that state on 2026-09-29 while `grove-prod-pg`
carried three rules -- and its allowlist had been CODIFIED all along: it is
destroyed every release train as a DEPENDENT of the droplet named in the
teardown's `-target`. Nothing was misconfigured and nothing alerted.

The port set is `SENSITIVE_PORTS` below, not just :22 (GOL-2572): the same
console click that opens SSH opens Postgres, and GOL-2582 showed a data store
sitting on a default-open perimeter for a whole release train with nobody
looking. 80 and 443 are absent from that table on purpose -- they are
world-open by design on the two prod firewalls, and a check that alarms on the
front door on day one is a check everyone mutes.

Findings carry a SEVERITY keyed on whether anything is actually behind the
firewall today: ERROR when it holds droplets or carries a `tags` entry (tag
firewalls auto-adopt), WARN when it is inert. Both exit non-zero -- the split
routes urgency, it does not excuse the exposure.

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
     and (with --census) every live droplet is covered, no firewall opens a
     sensitive port to the world, and every database cluster is narrowed
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

# The same idea on the database side, and NOT the same literal set. A
# trusted-source `ip_addr` rule holds a BARE address, not a CIDR -- the qa and
# prod configs `split("/", ...)` precisely for that -- so "allow everything"
# written by an operator reaching for the obvious thing is `0.0.0.0`, and DO
# accepts it. A superset of WORLD on purpose: a maskless 0.0.0.0 here is every
# bit as open as 0.0.0.0/0 and must not slip through a string compare against
# the droplet-side set.
WORLD_DB = WORLD | {"0.0.0.0", "::"}
ISSUE_REF = re.compile(r"^GOL-\d+$")

# Ports that must never answer the whole internet. Each one is either a service
# this stack actually runs or the classic thing a stray droplet leaves listening
# -- the value is what a reviewer needs to judge a hit without a search engine.
# A bare `:22` check was the first cut (GOL-2576) and it is not enough: the same
# click that opens SSH opens Postgres, and GOL-2582 proved a data store can sit
# on a default-open perimeter for a whole release train without anyone noticing.
SENSITIVE_PORTS = {
    22: "SSH",
    2375: "Docker daemon, plaintext + unauthenticated",
    2376: "Docker daemon, TLS",
    3000: "Grafana / Next.js dev server",
    3306: "MySQL",
    3389: "RDP",
    5080: "OpenObserve ingest + UI (GOL-2323)",
    5432: "PostgreSQL",
    5984: "CouchDB",
    6379: "Redis / KeyDB",
    8069: "Odoo direct -- bypasses nginx, TLS and Cloudflare",
    8072: "Odoo longpolling",
    9000: "MinIO / Portainer",
    9090: "Prometheus",
    9200: "Elasticsearch",
    11211: "memcached",
    25060: "DigitalOcean managed database",
    27017: "MongoDB",
}

# 80 and 443 are world-open ON PURPOSE on grove-prod-odoo-fw and
# grove-prod-blogs-fw -- that is the product. They are ABSENT FROM THE TABLE
# rather than allowlisted, deliberately: every allowlist entry here must carry
# an owner and an `expires` date, and these two exposures are permanent by
# design. A never-expiring allowlist line would be a lie about what the
# allowlist means, and a census that alarms on the front door on day one is a
# census everybody learns to ignore. A rule that reaches 80/443 *and* something
# in the table above (`ports: "0"`, a wide range) still fires, on the table
# port -- which is the correct reading of that rule.
EXPECTED_WORLD_OPEN = frozenset({80, 443})

# `name = "literal"` with no ${...} interpolation. Anything interpolated (the
# preview env's "${local.name}-fw", a per-PR ephemeral) is not statically
# resolvable, so we SKIP it rather than guess -- a wrong guess here would
# either false-alarm nightly or, worse, quietly "pass" the wrong firewall.
LITERAL_NAME = re.compile(r'^\s*name\s*=\s*"([^"$]*)"\s*$', re.M)
# A managed droplet: `digitalocean_droplet.odoo.id`. `(?<!\.)` keeps this from
# also matching the `data.digitalocean_droplet.x` form below and inventing a
# managed resource that does not exist.
DROPLET_REF = re.compile(r"(?<!\.)\bdigitalocean_droplet\.([A-Za-z0-9_-]+)\.id")
# A data-source droplet, as the holding-action firewalls for the un-codified
# snowflakes use (GOL-2566 legacy Ghost, GOL-2569 agent plane): those boxes are
# not Terraform-managed, so the firewall resolves them by NAME through
# `data "digitalocean_droplet"`. The `[0]` / `[count.index]` index is what the
# managed-resource pattern above cannot match, and an unmatched ref used to make
# the whole firewall a silent SKIP -- i.e. the two firewalls added specifically
# to close an internet-wide :22 would have been the only ones nothing watched.
DATA_DROPLET_REF = re.compile(
    r"\bdata\.digitalocean_droplet\.([A-Za-z0-9_-]+)(?:\[[^\]]*\])?\.id"
)
MODULE_REF = re.compile(r"module\.([A-Za-z0-9_-]+)\.droplet_id")
# One level of nesting matters: `[data.digitalocean_droplet.x[0].id]` -- a
# non-greedy `.*?\]` stops at the INDEX bracket and truncates the ref before
# its `.id`, which is why the data-source form silently resolved to nothing.
DROPLET_IDS = re.compile(r"droplet_ids\s*=\s*\[((?:[^\[\]]|\[[^\[\]]*\])*)\]", re.S)
# `name = var.legacy_ghost_droplet_name` -> resolve through the env's variable
# defaults. Only a literal string default resolves; anything else stays unknown.
VAR_REF_NAME = re.compile(r'^\s*name\s*=\s*var\.([A-Za-z0-9_-]+)\s*$', re.M)
VAR_DEFAULT = re.compile(r'^\s*default\s*=\s*"([^"$]*)"\s*$', re.M)
COUNT_META = re.compile(r'^\s*count\s*=\s*(.+?)\s*$', re.M)


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


def literal_name(body: str, var_defaults: dict[str, str] | None = None) -> str | None:
    # First top-level `name = "..."`. Sub-blocks get scanned too, but every
    # resource we care about declares its own name before any sub-block, and
    # a firewall's inbound_rule/outbound_rule carry no `name` at all.
    m = LITERAL_NAME.search(body)
    if m:
        return m.group(1)
    # `name = var.x` with a literal string default in this env's variables.tf.
    # Deliberately NOT a general interpolation resolver: a tfvars file or -var
    # on the command line overrides the default, so this is a best effort that
    # is right for how this repo uses it (the droplet-name vars exist to avoid
    # pinning a bare id, and their defaults ARE the live names). If a caller
    # overrides one, the name lookup simply fails to match live and reports.
    if var_defaults:
        vm = VAR_REF_NAME.search(body)
        if vm:
            return var_defaults.get(vm.group(1))
    return None


def parse_env(env_dir: Path):
    """-> (droplets, firewalls). Keys are Terraform addresses."""
    droplets: dict[str, str | None] = {}
    firewalls: dict[str, dict] = {}
    # Two passes: `variable` defaults first, because a droplet/data block that
    # names itself `var.x` needs them, and HCL has no file ordering guarantee.
    var_defaults: dict[str, str] = {}
    for tf in sorted(env_dir.glob("*.tf")):
        for header, body in iter_blocks(tf.read_text()):
            vm = re.match(r'variable\s+"([^"]+)"', header)
            if vm:
                dm = VAR_DEFAULT.search(body)
                if dm:
                    var_defaults[vm.group(1)] = dm.group(1)
    for tf in sorted(env_dir.glob("*.tf")):
        text = tf.read_text()
        for header, body in iter_blocks(text):
            rm = re.match(r'resource\s+"([^"]+)"\s+"([^"]+)"', header)
            dm = re.match(r'data\s+"([^"]+)"\s+"([^"]+)"', header)
            mm = re.match(r'module\s+"([^"]+)"', header)
            if rm and rm.group(1) == "digitalocean_droplet":
                droplets[f"digitalocean_droplet.{rm.group(2)}"] = literal_name(body, var_defaults)
            elif dm and dm.group(1) == "digitalocean_droplet":
                # Un-codified box fenced by a holding-action firewall: the data
                # source resolves it BY NAME, so the name is all we need and it
                # is looked up live exactly like a managed droplet's.
                droplets[f"data.digitalocean_droplet.{dm.group(2)}"] = literal_name(body, var_defaults)
            elif mm:
                # A module block is only interesting if some firewall
                # references its droplet_id; recorded unconditionally, cheap.
                droplets[f"module.{mm.group(1)}"] = literal_name(body, var_defaults)
            elif rm and rm.group(1) == "digitalocean_firewall":
                ids = DROPLET_IDS.search(body)
                refs = []
                if ids:
                    refs = [f"digitalocean_droplet.{m}" for m in DROPLET_REF.findall(ids.group(1))]
                    refs += [f"data.digitalocean_droplet.{m}" for m in DATA_DROPLET_REF.findall(ids.group(1))]
                    refs += [f"module.{m}" for m in MODULE_REF.findall(ids.group(1))]
                cm = COUNT_META.search(body)
                firewalls[f"digitalocean_firewall.{rm.group(2)}"] = {
                    "fw_name": literal_name(body, var_defaults),
                    "refs": refs,
                    "file": tf.name,
                    # A `count`-gated firewall legitimately may not exist: the
                    # holding-action fences default their gate to false so that
                    # MERGING the file is not an apply. Absence is then the
                    # expected state, not drift -- but the moment the firewall
                    # does exist live, its membership is asserted like any other.
                    "count": cm.group(1) if cm else None,
                }
    return droplets, firewalls


def api(path: str, token: str):
    req = urllib.request.Request(
        f"{API}/{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def covered_sensitive_ports(ports) -> set:
    """Which SENSITIVE_PORTS does a DO rule's `ports` value actually reach?

    DO renders "every port" as the string "0" (the UI says "all"), a single
    port as "22", and a range as "20-30". Matching only literals would wave
    through the strictly WORSE `ports: "0"` -- every port from anywhere -- so
    all three forms are decoded here. An unparseable value is treated as
    covering everything: not provably safe is not safe.
    """
    if ports is None:
        # No `ports` key at all is how DO renders protocol icmp, which carries
        # no port. Nothing to do with any service.
        return set()
    ports = str(ports).strip()
    if ports in ("0", "all", ""):
        return set(SENSITIVE_PORTS)
    if "-" in ports:
        lo, _, hi = ports.partition("-")
        try:
            lo, hi = int(lo), int(hi)
        except ValueError:
            return set(SENSITIVE_PORTS)
        return {p for p in SENSITIVE_PORTS if lo <= p <= hi}
    try:
        return {int(ports)} & set(SENSITIVE_PORTS)
    except ValueError:
        return set(SENSITIVE_PORTS)


def ports_cover_22(ports) -> bool:
    """Back-compatible shorthand: does this rule reach SSH specifically?"""
    return 22 in covered_sensitive_ports(ports)


def describe_ports(ports: set) -> str:
    """`{22, 5432}` -> `22/SSH, 5432/PostgreSQL`, truncated so a `ports: "0"`
    hit does not push the actionable part of the line off a Discord embed."""
    named = [f"{p}/{SENSITIVE_PORTS[p]}" for p in sorted(ports)]
    if len(named) > 4:
        return ", ".join(named[:4]) + f", +{len(named) - 4} more"
    return ", ".join(named)


def load_allowlist():
    """-> (droplet_exempt, firewall_exempt, database_exempt, problems).

    Entries are keyed by DO id and MUST carry an `issue` (GOL-NNNN) and a
    dated `expires`. A malformed or expired entry does not suppress anything
    and is itself reported -- an exemption that outlives its review is how a
    "temporary" exposure becomes permanent.

    A firewall exemption may carry an optional `ports` list to scope itself to
    the ports a human actually looked at: accepting `General`'s dormant :22 for
    a fortnight must not also pre-accept a 5432 somebody adds to it next week.
    Omitting `ports` exempts every sensitive port on that firewall and says so
    on the finding line, because that is the blunter instrument. `ports` is
    meaningless on the other two sections -- a database cluster exposes one
    port and an uncovered droplet exposes all of them -- so it is validated
    wherever it appears but only consulted for firewalls.
    """
    problems: list[str] = []
    if not ALLOWLIST.exists():
        return {}, {}, {}, problems
    try:
        raw = json.loads(ALLOWLIST.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return None, None, None, [f"allowlist {ALLOWLIST.name} is unreadable: {e}"]

    today = dt.date.today()
    out: dict[str, dict] = {
        "uncovered_droplets": {},
        "open_port_firewalls": {},
        "open_databases": {},
    }
    # `open_ssh_firewalls` was this section's name while the census only knew
    # about :22. Still read, so an existing entry does not silently stop
    # suppressing the moment the port table widened underneath it.
    sections = {
        "uncovered_droplets": ["uncovered_droplets"],
        "open_port_firewalls": ["open_port_firewalls", "open_ssh_firewalls"],
        "open_databases": ["open_databases"],
    }
    for section, keys in sections.items():
        for key_name in keys:
            for entry in raw.get(key_name, []) or []:
                key = entry.get("id")
                issue = str(entry.get("issue", ""))
                expires = str(entry.get("expires", ""))
                if key in (None, ""):
                    problems.append(f"{key_name}: entry with no `id`: {entry!r}")
                    continue
                if not ISSUE_REF.match(issue):
                    problems.append(
                        f"{key_name}[{key}]: `issue` must be GOL-NNNN, got {issue!r}"
                    )
                    continue
                try:
                    when = dt.date.fromisoformat(expires)
                except ValueError:
                    problems.append(
                        f"{key_name}[{key}]: `expires` must be YYYY-MM-DD, got {expires!r}"
                    )
                    continue
                scope = entry.get("ports")
                if scope is not None:
                    # `isinstance(scope, list)` first: a bare string is
                    # iterable, so `[int(p) for p in "22"]` quietly yields
                    # [2, 2] -- an exemption for two ports nobody named.
                    try:
                        if not isinstance(scope, list):
                            raise TypeError(scope)
                        entry = dict(entry, ports=[int(p) for p in scope])
                    except (TypeError, ValueError):
                        problems.append(
                            f"{key_name}[{key}]: `ports` must be a list of numbers, got {scope!r}"
                        )
                        continue
                if when < today:
                    problems.append(
                        f"{key_name}[{key}] ({entry.get('name', '?')}): exemption EXPIRED "
                        f"{expires} -- re-review {issue} or fix the exposure"
                    )
                    continue
                out[section][str(key)] = entry
    return (
        out["uncovered_droplets"],
        out["open_port_firewalls"],
        out["open_databases"],
        problems,
    )


def census(live_droplets, live_fws, live_dbs=()):
    """Account-wide, config-independent. -> (n_errors, n_warnings).

    Deliberately asks the ACCOUNT, not the repo. The per-env check compares
    live membership against Terraform, so it can only ever see droplets
    Terraform declares; this one enumerates what actually exists and demands
    that each box be behind something and that nothing sensitive faces the
    whole internet.

    SEVERITY is keyed on whether the exposure is load-bearing TODAY:

      ERROR  something is actually reachable -- an uncovered droplet, a
             database cluster that narrows nothing, or a
             world-open sensitive port on a firewall that holds droplets or
             carries a `tags` entry (a tag firewall AUTO-ADOPTS any droplet
             wearing that tag, so "no droplet_ids" is not "nothing attached").
      WARN   a loaded gun: the same rule on a firewall with neither droplets
             nor tags. Inert today, live the moment a human attaches it -- and
             `General` (GOL-2570) is named like a default, so that human click
             is the likely one.

    BOTH exit non-zero. The split routes urgency, it does not gate the exit:
    a nightly check that stays green while a world-open :22 firewall sits on
    the account is the same silence GOL-2565 was made of. If an inert exposure
    is genuinely accepted, that belongs in the allowlist with an owner and an
    expiry, not in the exit code.

    `live_dbs` is a sequence of (cluster, rules_or_None) pairs as returned by
    `fetch_database_firewalls` -- pairs rather than raw clusters because the
    trusted-source list is a second API call per cluster, and keeping the
    fetch in the caller keeps this function pure and offline-testable.
    `rules is None` means that read FAILED, which is reported rather than
    skipped. There is no WARN tier for the "open" database findings, for the
    same reason an uncovered droplet has none: a managed cluster always has a
    live public host, so there is no dormant version of the exposure.
    """
    droplet_exempt, fw_exempt, db_exempt, problems = load_allowlist()
    if droplet_exempt is None:
        for msg in problems:
            print(f"  ! census: {msg}")
        return 1, 0

    print(
        f"\n=== census: {len(live_droplets)} live droplet(s), "
        f"{len(live_fws)} live firewall(s), {len(live_dbs)} database cluster(s)"
    )
    errors = warns = 0
    # A broken or lapsed allowlist is an ERROR, not a WARN: it means the file
    # that decides what gets suppressed can no longer be trusted to suppress
    # only what a human approved.
    for msg in problems:
        print(f"  ERROR ALLOWLIST {msg}")
        errors += 1

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
        # Always ERROR: an uncovered droplet is a real box with a real public
        # IP right now. There is no inert version of this finding.
        print(
            f"  ERROR UNCOVERED droplet {name} (id {did}, created {d.get('created_at', '?')}, "
            f"region {d.get('region', {}).get('slug', '?')}): in NO cloud firewall"
        )
        errors += 1

    # The reverse hazard: a world-open rule on a sensitive port. Checked on
    # every firewall whether or not it currently holds a droplet, because a
    # dormant rule is one console click (or one matching tag) from live.
    # Aggregated per firewall so a `ports: "0"` rule is one finding naming
    # every service it reaches, not a wall of near-identical lines.
    for f in sorted(live_fws, key=lambda x: x["name"]):
        hits: dict[int, set[str]] = {}
        for rule in f.get("inbound_rules") or []:
            if rule.get("protocol") not in ("tcp", "all"):
                continue
            world = set((rule.get("sources") or {}).get("addresses") or []) & WORLD
            if not world:
                continue
            for p in covered_sensitive_ports(rule.get("ports")):
                hits.setdefault(p, set()).update(world)
        if not hits:
            continue

        droplet_ids = f.get("droplet_ids") or []
        tags = f.get("tags") or []
        where = f"{len(droplet_ids)} droplet(s), {len(tags)} tag(s)"
        exempt = fw_exempt.get(str(f["id"]))
        if exempt:
            scope = exempt.get("ports")
            exempted = set(hits) if scope is None else set(scope)
            granted = {p for p in hits if p in exempted}
            if granted:
                print(
                    f"  ALLOWED firewall {f['name']} ({f['id']}): world-open "
                    f"{describe_ports(granted)}, exempt until {exempt['expires']} per "
                    f"{exempt['issue']}"
                    + ("" if scope is not None else " (UNSCOPED -- covers every sensitive port)")
                )
            if exempt.get("name") and exempt["name"] != f["name"]:
                print(
                    f"  ! allowlist entry for {f['id']} says name {exempt['name']!r}, "
                    f"live name is {f['name']!r}"
                )
            hits = {p: v for p, v in hits.items() if p not in exempted}
            if not hits:
                continue

        world_srcs = sorted(set().union(*hits.values()))
        # tags non-empty is ERROR even with droplet_ids empty: a tag firewall
        # auto-adopts, so its membership is whatever wears the tag tomorrow.
        if droplet_ids or tags:
            print(
                f"  ERROR firewall {f['name']} (id {f['id']}, {where}): world-open "
                f"{describe_ports(set(hits))} <- {world_srcs}"
                + ("" if droplet_ids else " -- attaches BY TAG, so membership is whatever wears the tag")
            )
            errors += 1
        else:
            print(
                f"  WARN firewall {f['name']} (id {f['id']}, {where}): world-open "
                f"{describe_ports(set(hits))} <- {world_srcs}"
                " -- dormant today, world-open the moment anything attaches"
            )
            warns += 1

    # The third surface (GOL-2582): managed-database trusted sources. A
    # different API object and a different Terraform resource from everything
    # above, so nothing above can see it. Every cluster is checked on EVERY
    # run, prod included -- the exposure that got found was in QA, but a check
    # that only watches the box that was wrong once is how the next one gets
    # missed (GOL-2582 item 3).
    live_droplet_ids = {str(d["id"]) for d in live_droplets}
    for cluster, rules in sorted(live_dbs, key=lambda x: x[0].get("name") or ""):
        cid, cname = str(cluster.get("id")), cluster.get("name") or "?"
        engine = cluster.get("engine") or "?"
        exempt = db_exempt.get(cid)

        if rules is None:
            # Fail closed, and keep it distinguishable from "read fine, no
            # rules". Conflating the two would either invent a finding or hide
            # one. An exemption deliberately does NOT cover this: the entry
            # records that a human accepted a KNOWN exposure, and this posture
            # is not known. ERROR rather than WARN because a census that
            # silently skips a surface is the GOL-2565 silence.
            print(
                f"  ERROR UNREADABLE-DB database {cname} ({cid}, {engine}): could not read "
                f"its trusted sources -- posture UNVERIFIED, treating as a finding"
            )
            errors += 1
            continue

        # Non-empty is NOT the test. One `ip_addr: 0.0.0.0` rule is a longer
        # list and a wider hole, so the CONTENT is what gets judged.
        world = {
            str(r.get("value"))
            for r in rules
            if r.get("type") == "ip_addr" and str(r.get("value")) in WORLD_DB
        }
        if not rules:
            problem = (
                "trusted sources are EMPTY -- on DO this is NOT closed, the public "
                "host answers :25060 for ANY source presenting credentials"
            )
        elif world:
            problem = f"trusted sources include the whole internet: {sorted(world)}"
        else:
            problem = None

        if problem is not None:
            if exempt:
                print(
                    f"  ALLOWED database {cname} ({cid}): {problem}; exempt until "
                    f"{exempt['expires']} per {exempt['issue']}"
                )
                if exempt.get("name") and exempt["name"] != cname:
                    print(
                        f"  ! allowlist entry for {cid} says name {exempt['name']!r}, "
                        f"live name is {cname!r}"
                    )
                continue
            print(f"  ERROR OPEN-DB database {cname} (id {cid}, {engine}): {problem}")
            errors += 1
            continue

        # Narrowed. Print the rules rather than just the count: this is what
        # makes prod's allowlist ASSERTED on every run instead of assumed to
        # still be whatever it was the day somebody last looked.
        shape = ", ".join(sorted(f"{r.get('type')}:{r.get('value')}" for r in rules))
        print(
            f"  OK database {cname} ({cid}, {engine}): {len(rules)} trusted source(s) "
            f"-- {shape}"
        )
        if exempt:
            # A cluster that is fine no longer needs cover. Say so, or the
            # allowlist only ever grows and stops meaning anything.
            print(
                f"  ! allowlist still exempts {cname} ({cid}) per {exempt['issue']}, "
                f"but it is CLEAN now -- drop the entry"
            )

        # A `droplet` rule naming a droplet that no longer exists is PRESENT
        # but not load-bearing -- the cluster is narrower than its rule list
        # reads, and the rule is the exact shape GOL-2582 was about: a
        # trusted source hanging off a droplet's lifetime. WARN, not ERROR:
        # nothing is reachable through a dead id (DO does not recycle them),
        # but the next person to debug "QA cannot reach the database" is one
        # step from widening the list by hand. `tag` rules are deliberately
        # NOT resolved the same way -- a tag with nobody wearing it yet is the
        # normal state between release trains, and alarming on it nightly is
        # how a watcher gets muted.
        dangling = sorted(
            str(r.get("value"))
            for r in rules
            if r.get("type") == "droplet" and str(r.get("value")) not in live_droplet_ids
        )
        if dangling:
            print(
                f"  WARN database {cname} ({cid}): trusted-source droplet rule(s) "
                f"{dangling} name no live droplet -- the rule is dead weight and the "
                f"cluster is narrower than its list reads"
            )
            warns += 1

    if not errors and not warns:
        print(
            "  OK: every live droplet is behind a firewall, no firewall opens a "
            "sensitive port to the world, and every database cluster is narrowed"
        )
    else:
        print(f"  census: {errors} error(s), {warns} warning(s)")
    return errors, warns


def fetch_database_firewalls(token):
    """-> ([(cluster, rules_or_None)], fatal_error_or_None).

    Two calls: list the clusters, then read each one's trusted sources. A
    per-cluster read that fails yields `None` rather than `[]` -- conflating
    "could not read" with "read fine, no rules" would either invent a finding
    or, worse, hide one. Losing the cluster LIST is different in kind and is
    reported as fatal by the caller: the leg would have run blind, and a census
    that silently skips a whole surface is the failure this check exists to
    prevent.
    """
    try:
        clusters = api("databases?per_page=200", token).get("databases") or []
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
        return [], f"could not list database clusters: {e}"

    out = []
    for c in clusters:
        try:
            rules = api(f"databases/{c['id']}/firewall", token).get("rules") or []
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
            rules = None
        out.append((c, rules))
    return out, None


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
    # 22 and would sail past a literal == "22" match.  `ports_cover_22` is now
    # a thin wrapper over `covered_sensitive_ports`, so this table pins both.
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

    def run(droplets, fws, allowlist=None, dbs=()):
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
                    errors, warns = census(droplets, fws, dbs)
            finally:
                ALLOWLIST = saved
        return bool(errors or warns), buf.getvalue()

    def severity(droplets, fws, allowlist=None, dbs=()):
        """-> (n_errors, n_warnings). The split is the point of the check, so
        it is asserted directly and not inferred from the printed prefix."""
        global ALLOWLIST
        saved = ALLOWLIST
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            if allowlist is not None:
                path.write_text(json.dumps(allowlist))
            ALLOWLIST = path
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    return census(droplets, fws, dbs)
            finally:
                ALLOWLIST = saved

    def _db(cid, name, engine="pg"):
        return {"id": cid, "name": name, "engine": engine}

    def _dbrule(rtype, value):
        return {"type": rtype, "value": value}

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

    # -- open-port detection, attached or not
    found, out = run([], [_fw("open", rules=[_rule()])])
    check(":22 from 0.0.0.0/0 is a finding", found, True)
    check("open port names the firewall", "firewall open" in out, True)
    check("open port names the service", "22/SSH" in out, True)
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
    found, _ = run([], [_fw("http", rules=[_rule(ports="80")])])
    check(":80 from the world is not this check's business", found, False)

    # -- the widened table (GOL-2572). A bare :22 check waved all of these
    # through, and every one of them is a data store or an admin plane.
    for port in (5432, 3306, 6379, 27017, 9090, 3000, 8069, 25060, 2375):
        found, out = run([], [_fw("wide", rules=[_rule(ports=str(port))])])
        check(f":{port} from the world is a finding", found, True)
        check(f":{port} is named with its service", f"{port}/" in out, True)

    # 80/443 must never migrate into the table by accident: the two prod
    # firewalls carry them world-open by design, so the day they collide the
    # census goes red on production's front door.
    check(
        "the table never overlaps the intentional 80/443 exposure",
        set(SENSITIVE_PORTS) & EXPECTED_WORLD_OPEN,
        set(),
    )

    # -- `ports: "0"` reaches everything, and is reported as ONE aggregated
    # finding rather than one line per port.
    check("all-ports covers the whole table", covered_sensitive_ports("0"), set(SENSITIVE_PORTS))
    found, out = run([], [_fw("wide", rules=[_rule(ports="0")])])
    check("all-ports is a finding", found, True)
    check("all-ports is truncated, not a wall", "more" in out, True)
    check("all-ports is one finding line",
          len([l for l in out.splitlines() if "world-open" in l]), 1)

    # -- a range picks up only what it spans
    check("range 5000-6000 covers 5080+5432+5984", covered_sensitive_ports("5000-6000"),
          {5080, 5432, 5984})
    check("range 100-200 covers nothing sensitive", covered_sensitive_ports("100-200"), set())

    # -- SEVERITY. Attached is live; unattached is a loaded gun. Tags count as
    # attached because a tag firewall auto-adopts whatever wears the tag.
    check(
        "attached world-open port is an ERROR",
        severity([], [_fw("live", droplet_ids=[1], rules=[_rule()])]),
        (1, 0),
    )
    check(
        "tag-attached world-open port is an ERROR, not a WARN",
        severity([], [_fw("bytag", tags=["prod"], rules=[_rule()])]),
        (1, 0),
    )
    check(
        "dormant world-open port is a WARN",
        severity([], [_fw("dormant", rules=[_rule()])]),
        (0, 1),
    )
    check(
        "an uncovered droplet is always an ERROR",
        severity([_droplet(1, "a")], []),
        (1, 0),
    )
    # A WARN still exits non-zero: a nightly that stays green while a
    # world-open :22 firewall sits on the account is the GOL-2565 silence.
    found, _ = run([], [_fw("dormant", rules=[_rule()])])
    check("a WARN is still a non-zero finding", found, True)
    out_err, out_warn = severity([], [_fw("bytag", tags=["p"], rules=[_rule()])])
    check("ERROR is reported as an error, not both", (out_err, out_warn), (1, 0))

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
        {"open_port_firewalls": [{"id": "u-1", "name": "open", "issue": "GOL-1", "expires": future}]},
    )
    check("valid fw exemption suppresses", found, False)
    check("unscoped exemption says so", "UNSCOPED" in out, True)

    # -- the pre-widening section name still suppresses, so an entry written
    # when this check only knew about :22 does not lapse silently.
    found, _ = run(
        [], [_fw("open", fid="u-1", rules=[_rule()])],
        {"open_ssh_firewalls": [{"id": "u-1", "name": "open", "issue": "GOL-1", "expires": future}]},
    )
    check("legacy open_ssh_firewalls section still suppresses", found, False)

    # -- a PORT-SCOPED exemption covers what a human looked at and nothing else.
    # Accepting a dormant :22 must not pre-accept a 5432 added to it next week.
    scoped = {"open_port_firewalls": [
        {"id": "u-1", "name": "open", "issue": "GOL-1", "expires": future, "ports": [22]}
    ]}
    found, _ = run([], [_fw("open", fid="u-1", rules=[_rule()])], scoped)
    check("port-scoped exemption suppresses its own port", found, False)
    found, out = run(
        [], [_fw("open", fid="u-1", rules=[_rule(), _rule(ports="5432")])], scoped
    )
    check("port-scoped exemption does NOT cover another port", found, True)
    check("the unexempted port is the one named", "5432/PostgreSQL" in out, True)
    check("the exempted port is not re-reported", "22/SSH <-" not in out, True)

    found, out = run(
        [], [_fw("open", fid="u-1", rules=[_rule()])],
        {"open_port_firewalls": [
            {"id": "u-1", "name": "open", "issue": "GOL-1", "expires": future, "ports": "22"}
        ]},
    )
    check("non-list `ports` does not suppress", found, True)
    check("non-list `ports` is reported", "must be a list of numbers" in out, True)

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

    # ---- managed databases (GOL-2582 / GOL-2913) ---------------------------
    # Pinned against the REAL API payloads, extra keys and all: the live
    # response carries `uuid`, `cluster_uuid` and `created_at` on every rule,
    # and a decode that only tolerates the three keys the finding needs would
    # pass here and break on the account. Read read-only 2026-10-02 from
    # GET /v2/databases/{id}/firewall for both clusters.
    PROD_RULES = [
        {
            "uuid": "43655cb7-eeaf-4b8a-878f-bc16d93bf968",
            "cluster_uuid": "fe81c735-d0f8-47ed-9f28-47d2542d2e01",
            "type": "ip_addr",
            "value": "173.84.140.152",
            "created_at": "2026-09-16T14:53:57Z",
        },
        {
            "uuid": "516bb147-197d-4657-86ac-37beb1ad175f",
            "cluster_uuid": "fe81c735-d0f8-47ed-9f28-47d2542d2e01",
            "type": "droplet",
            "value": "601081550",
            "created_at": "2026-09-16T14:53:57Z",
        },
        {
            "uuid": "e226d032-02f2-4b31-a8c4-1fccc12d8b6b",
            "cluster_uuid": "fe81c735-d0f8-47ed-9f28-47d2542d2e01",
            "type": "ip_addr",
            "value": "74.47.41.38",
            "created_at": "2026-09-16T14:53:57Z",
        },
    ]
    QA_RULES = [
        {
            "uuid": "b4ab16e6-b709-4397-bba7-aaec5eb2d328",
            "cluster_uuid": "44e19536-269a-40a5-be28-e1d554d20831",
            "type": "ip_addr",
            "value": "173.84.140.152",
            "created_at": "2026-09-29T19:21:31Z",
        },
        {
            "uuid": "bd31ff9e-1a70-4823-bdb8-c02a44623e71",
            "cluster_uuid": "44e19536-269a-40a5-be28-e1d554d20831",
            "type": "ip_addr",
            "value": "74.47.41.38",
            "created_at": "2026-09-29T19:21:31Z",
        },
    ]
    PROD_DB = _db("fe81c735-d0f8-47ed-9f28-47d2542d2e01", "grove-prod-pg")
    QA_DB = _db("44e19536-269a-40a5-be28-e1d554d20831", "grove-qa-l3-pg")
    # grove-prod-odoo, the droplet prod's `droplet` rule names, and the
    # firewall that covers it -- otherwise the droplet leg reports it as
    # UNCOVERED and the database assertions below get a borrowed error.
    PROD_ODOO = _droplet(601081550, "grove-prod-odoo")
    PROD_FW = [_fw("grove-prod-odoo-fw", fid="fc0ff910", droplet_ids=[601081550])]

    # THE WHOLE REASON THE LEG EXISTS. DO renders "open to anyone with the
    # password" as an EMPTY list, which reads like "closed" to anything that
    # only tests for the presence of the firewall object. Pinned first and
    # hardest, in the state grove-qa-l3-pg was actually found in.
    found, out = run([], [], {}, dbs=[(QA_DB, [])])
    check("empty trusted sources is a finding", found, True)
    check("empty trusted sources names the cluster", "OPEN-DB database grove-qa-l3-pg" in out, True)
    check("empty trusted sources explains DO's semantics", "NOT closed" in out, True)
    check(
        "an open cluster is an ERROR, never a WARN -- there is no dormant DB",
        severity([], [], {}, dbs=[(QA_DB, [])]),
        (1, 0),
    )

    # Both clusters' real shapes are clean, and the rules are PRINTED, so
    # prod's allowlist is asserted on every run rather than assumed to still
    # be whatever it was the day somebody last looked (GOL-2582 item 3).
    found, out = run([PROD_ODOO], PROD_FW, {}, dbs=[(PROD_DB, PROD_RULES), (QA_DB, QA_RULES)])
    check("both live clusters' real shapes are clean", found, False)
    check("prod's droplet rule is printed", "droplet:601081550" in out, True)
    check("prod's ip rules are printed", "ip_addr:173.84.140.152" in out, True)
    check("the qa cluster is reported too, not just prod", "grove-qa-l3-pg" in out, True)
    check("the header counts the clusters", "2 database cluster(s)" in out, True)

    # Losing prod's rules is the same finding as QA's was. The leg is not
    # keyed on which cluster was wrong once.
    check(
        "prod going empty is an ERROR like any other cluster",
        severity([PROD_ODOO], PROD_FW, {}, dbs=[(PROD_DB, []), (QA_DB, QA_RULES)]),
        (1, 0),
    )

    # Non-empty is NOT the test: one world-open rule is a longer list and a
    # wider hole. The MASKLESS forms matter most -- a trusted-source ip_addr
    # rule holds a bare address, so 0.0.0.0 is what an operator would actually
    # type, and matching only the droplet-side WORLD set would wave it through.
    for value in ["0.0.0.0/0", "0.0.0.0", "::/0", "::"]:
        found, out = run([], [], {}, dbs=[(_db("c-w", "wide"), [_dbrule("ip_addr", value)])])
        check(f"ip_addr {value} is world-open", found, True)
        check(f"ip_addr {value} is reported as such", "whole internet" in out, True)
    check(
        "WORLD_DB is a strict superset of the droplet-side WORLD",
        WORLD_DB > WORLD,
        True,
    )

    # ...and a narrow rule that merely looks broad is not a finding.
    found, _ = run([], [], {}, dbs=[(_db("c-p", "priv"), [_dbrule("ip_addr", "10.0.0.0")])])
    check("a private ip_addr is not world-open", found, False)
    # A world-open value on a non-ip_addr rule type is not an address at all --
    # `droplet`/`tag`/`k8s`/`app` values are ids and names, so a string compare
    # against them would be a category error, not a check.
    found, _ = run([], [], {}, dbs=[(_db("c-t", "tagged"), [_dbrule("tag", "0.0.0.0")])])
    check("a tag literally named 0.0.0.0 is not an address", found, False)

    # A failed per-cluster read must not read as clean. `None` (could not read)
    # and `[]` (read fine, no rules) are different claims and only the second
    # is a statement about the cluster.
    found, out = run([], [], {}, dbs=[(_db("c-u", "unknown-posture"), None)])
    check("unreadable cluster firewall is a finding", found, True)
    check("unreadable cluster says UNVERIFIED", "UNVERIFIED" in out, True)
    check("unreadable is labelled apart from open", "UNREADABLE-DB" in out, True)
    check("unreadable is not reported as OPEN-DB", "OPEN-DB" in out, False)

    # A `droplet` rule naming a droplet that no longer exists: present, but
    # not load-bearing. WARN -- nothing is reachable through a dead id, but
    # the list reads narrower than it is, and this is the GOL-2582 shape (a
    # trusted source hanging off a droplet's lifetime).
    check(
        "a dangling droplet rule is a WARN, not an ERROR",
        severity([], [], {}, dbs=[(PROD_DB, PROD_RULES)]),
        (0, 1),
    )
    found, out = run([], [], {}, dbs=[(PROD_DB, PROD_RULES)])
    check("the WARN line itself names the id", "['601081550'] name no live droplet" in out, True)
    check("the dangling rule is called dead weight", "dead weight" in out, True)
    # ...and resolves clean the moment that droplet is in the live list, which
    # is the state of the account today.
    check(
        "the same rules are clean when the droplet is live",
        severity([PROD_ODOO], PROD_FW, {}, dbs=[(PROD_DB, PROD_RULES)]),
        (0, 0),
    )
    # A `tag` rule is deliberately NOT resolved this way: a tag nobody wears
    # yet is the normal state between release trains (the GOL-2582 fix is a
    # tag rule), and a nightly that alarms on it is a nightly that gets muted.
    check(
        "a tag rule with nobody wearing the tag is not a finding",
        severity([], [], {}, dbs=[(QA_DB, [_dbrule("tag", "pg-client")])]),
        (0, 0),
    )

    # Exemptions behave exactly as they do for droplets and firewalls...
    live_db_exempt = {
        "open_databases": [
            {"id": "c-qa", "name": "grove-qa-l3-pg", "issue": "GOL-2582", "expires": future}
        ]
    }
    found, out = run([], [], live_db_exempt, dbs=[(_db("c-qa", "grove-qa-l3-pg"), [])])
    check("a live db exemption suppresses", found, False)
    check("a live db exemption is announced", "ALLOWED database" in out, True)

    # ...including expiry: an accepted exposure that outlives its review is a
    # finding again, and the lapsed entry is itself reported.
    found, out = run(
        [], [],
        {"open_databases": [
            {"id": "c-qa", "name": "grove-qa-l3-pg", "issue": "GOL-2582", "expires": past}
        ]},
        dbs=[(_db("c-qa", "grove-qa-l3-pg"), [])],
    )
    check("an expired db exemption stops suppressing", found, True)
    check("an expired db exemption is reported", "EXPIRED" in out, True)
    check("an expired db exemption still surfaces the cluster", "OPEN-DB database" in out, True)

    # ...and the issue/date regexes, which are what stop an exemption being a
    # free-text excuse.
    found, out = run(
        [], [],
        {"open_databases": [{"id": "c-qa", "name": "grove-qa-l3-pg", "expires": future}]},
        dbs=[(_db("c-qa", "grove-qa-l3-pg"), [])],
    )
    check("an issue-less db exemption does not suppress", found, True)
    check("an issue-less db exemption is reported", "must be GOL-NNNN" in out, True)

    # An exemption covering a cluster that has since been fixed is dead weight.
    found, out = run(
        [PROD_ODOO], PROD_FW,
        {"open_databases": [
            {"id": PROD_DB["id"], "name": "grove-prod-pg", "issue": "GOL-2582", "expires": future}
        ]},
        dbs=[(PROD_DB, PROD_RULES)],
    )
    check("a clean cluster with an exemption is not a finding", found, False)
    check("a stale db exemption is flagged for pruning", "drop the entry" in out, True)

    # An UNREADABLE cluster must NOT be covered by an exemption: the entry
    # accepts a KNOWN exposure and this posture is unknown.
    found, _ = run(
        [], [],
        {"open_databases": [
            {"id": "c-u", "name": "unknown-posture", "issue": "GOL-2582", "expires": future}
        ]},
        dbs=[(_db("c-u", "unknown-posture"), None)],
    )
    check("an exemption does not cover an unreadable cluster", found, True)

    # A renamed cluster keeps its exemption but says so, like a droplet.
    found, out = run(
        [], [], live_db_exempt, dbs=[(_db("c-qa", "renamed-pg"), [])]
    )
    check("a db rename does not break the exemption", found, False)
    check("a db rename is flagged", "live name is 'renamed-pg'" in out, True)

    # No clusters at all must not print a false all-clear about databases --
    # and must not invent a finding either.
    found, out = run([_droplet(1, "a", tags=["p"])], [_fw("f", tags=["p"])])
    check("no clusters is not a finding", found, False)
    check("no clusters still says every cluster is narrowed", "is narrowed" in out, True)
    check("no clusters is counted as zero", "0 database cluster(s)" in out, True)

    # -- fetch_database_firewalls: the list/per-cluster split, offline.
    saved_api = globals()["api"]
    try:
        def fake_api(path, token):
            if path.startswith("databases?"):
                return {"databases": [PROD_DB, QA_DB]}
            if path == f"databases/{PROD_DB['id']}/firewall":
                return {"rules": PROD_RULES}
            raise urllib.error.HTTPError(path, 403, "forbidden", None, None)

        globals()["api"] = fake_api
        pairs, fatal = fetch_database_firewalls("t")
        check("a per-cluster failure is not fatal", fatal, None)
        check("every cluster is still returned", [c["name"] for c, _ in pairs],
              ["grove-prod-pg", "grove-qa-l3-pg"])
        check("the readable cluster keeps its rules", pairs[0][1], PROD_RULES)
        check("the unreadable cluster is None, not []", pairs[1][1], None)

        def dead_api(path, token):
            raise urllib.error.HTTPError(path, 500, "boom", None, None)

        globals()["api"] = dead_api
        pairs, fatal = fetch_database_firewalls("t")
        check("losing the cluster LIST is fatal", bool(fatal), True)
        check("the fatal names the surface", "database clusters" in (fatal or ""), True)
        check("a fatal returns no pairs to misread as 'no clusters'", pairs, [])
    finally:
        globals()["api"] = saved_api

    # -- an unreadable allowlist fails closed rather than exempting nothing quietly
    saved = ALLOWLIST
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "allow.json"
        bad.write_text("{ not json")
        ALLOWLIST = bad
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors, warns = census([], [])
        ALLOWLIST = saved
    check("unreadable allowlist is an ERROR, not a WARN", (errors, warns), (1, 0))

    # -- the real allowlist in this repo must itself be valid
    dex, fex, dbex, problems = load_allowlist()
    check(f"repo allowlist parses ({ALLOWLIST.name})", dex is not None, True)
    check(f"repo allowlist has no problems: {problems}", problems, [])
    # All THREE sections must load, not just the two the droplet legs read: a
    # typo'd `open_databases` key would otherwise exempt nothing in silence.
    check("repo allowlist exposes all three sections", (fex, dbex) != (None, None), True)

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
        "firewall, no firewall may open a sensitive port to 0.0.0.0/0 or ::/0, and "
        "every managed database cluster must have trusted sources that narrow "
        "something. Reads the DO "
        "account rather than the repo, so it sees UN-CODIFIED droplets the per-env "
        "check cannot (GOL-2576) and database trusted sources, which live behind a "
        "different API object entirely (GOL-2582).",
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
                if fw["count"] is not None:
                    print(
                        f"  SKIP {name}: count-gated (count = {fw['count']}) and not live "
                        f"-- gate is off / not applied yet, so absence is expected. "
                        f"Membership WILL be asserted once it exists."
                    )
                elif args.allow_absent:
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

    census_errors, census_warns = 0, 0
    if args.census:
        live_dbs, db_err = fetch_database_firewalls(token)
        if db_err:
            # The cluster list is the only thing that makes the database leg
            # possible at all. Losing it means the leg ran blind, and a census
            # reporting "0 database cluster(s)" when it simply could not ask is
            # indistinguishable from an account with no clusters -- so it is
            # fatal (exit 2, the "broken watcher" code the workflow alerts on
            # separately), not a quiet omission.
            print(f"::error::census: {db_err}", file=sys.stderr)
            return 2
        census_errors, census_warns = census(live_droplets, live_fws, live_dbs)
    census_found = bool(census_errors or census_warns)

    if not drift and not census_found:
        if args.envs:
            print("\nOK: every codified firewall contains the droplets its config names")
        return 0

    # Flush so the per-firewall report lands ahead of this banner when stdout
    # and stderr share a terminal or a CI log.
    sys.stdout.flush()
    if census_found:
        print(
            f"\nCensus finding: {census_errors} ERROR(s), {census_warns} WARN(s).\n"
            "An UNCOVERED droplet has no code behind it by definition -- the fix is to\n"
            "codify a firewall for it and apply, not to click one on:\n"
            "  infra/terraform/environments/production/<name>-fw.tf\n"
            "A world-open sensitive port should be narrowed to known /32s if the\n"
            "firewall is in use (ERROR), or the firewall deleted if it is dormant\n"
            "(WARN -- inert today, one console click from live).\n"
            "An OPEN-DB cluster needs a `digitalocean_database_firewall` in its env --\n"
            "and find out WHY it is empty before re-applying one. An allowlist that was\n"
            "codified all along can still be destroyed every train as a DEPENDENT of a\n"
            "droplet named in `-target` (GOL-2582); re-applying without fixing that just\n"
            "resets the clock. Prefer a `tag` rule over a droplet-id rule so the resource\n"
            "does not hang off the droplet's lifetime -- which is also what clears a\n"
            "WARN about a droplet rule naming no live droplet. UNREADABLE-DB is not a\n"
            "finding about the cluster: re-run, and if it persists check the DO token's\n"
            "database scope.\n"
            "If an exposure is genuinely accepted, add a dated, GOL-referencing entry to\n"
            f"  {ALLOWLIST.relative_to(Path.cwd()) if ALLOWLIST.is_relative_to(Path.cwd()) else ALLOWLIST}\n"
            "scoped with `ports` to what you actually reviewed, so the acceptance is a\n"
            "reviewed commit with an expiry, not silence.",
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
