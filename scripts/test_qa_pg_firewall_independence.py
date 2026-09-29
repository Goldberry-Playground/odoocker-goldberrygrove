#!/usr/bin/env python3
"""Regression guard: the QA Managed PG allowlist must not hang off the droplet.

GOL-2582. `grove-qa-l3-pg` was found with `trusted_sources: []` on 2026-09-29 --
which on DO managed databases is NOT "closed": the cluster's public host accepts
any source that presents credentials, so the whole perimeter is the password.

Nothing was missing. `digitalocean_database_firewall.pg` was codified all along
and was destroyed every release train, because:

  * its rule was `type = "droplet", value = digitalocean_droplet.odoo.id`;
  * that reference makes the firewall a DEPENDENT of the droplet;
  * `qa-l3-teardown.sh compute` destroys with `-target=digitalocean_droplet.odoo`,
    and `-target` destroys the target AND its dependents;
  * the cluster survives on `prevent_destroy`, so a live cluster is left holding
    an empty allowlist until the next bring-up.

The failure is invisible in review: the teardown script never names the firewall,
the destroy is implicit in `-target`, and re-introducing the droplet reference
looks like a tightening ("pin it to the actual box") rather than a loosening.
So the invariant is asserted here instead of trusted to a reader.

Static and offline on purpose -- it reads the HCL, so it fails on the PR that
reintroduces the reference rather than on the train that acts on it. The live
complement is the census leg of check-firewall-membership.py, which asks DO
whether the cluster actually has trusted sources today.

Scoped to qa-app-platform. Production's DB firewall may legitimately use a
droplet-id rule: there is no targeted-teardown script for prod, so the rule is
never in a `-target` blast radius.

    python3 scripts/test_qa_pg_firewall_independence.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAIN_TF = (
    Path(__file__).resolve().parents[1]
    / "infra/terraform/environments/qa-app-platform/main.tf"
)


def extract_block(text: str, header_re: str) -> str:
    """Return the body of the first block whose header matches, braces balanced.

    A brace-depth scan rather than a regex: `dynamic "rule" { ... }` nests, and a
    non-greedy match to the first `}` would stop inside it and read as though the
    outer block ended early -- which would make this guard pass on a config it
    never actually finished reading.
    """
    m = re.search(header_re, text)
    if not m:
        return ""
    i = text.index("{", m.start())
    depth, start = 0, i + 1
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j]
    return ""


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{label}{': ' + detail if detail else ''}")

    text = MAIN_TF.read_text()

    fw = extract_block(text, r'resource\s+"digitalocean_database_firewall"\s+"pg"\s*')
    check("digitalocean_database_firewall.pg block not found", bool(fw))
    if not fw:
        print("FAIL " + failures[0], file=sys.stderr)
        return 1

    # THE invariant. Any `digitalocean_droplet.<x>` reference inside the block --
    # in a rule value or anywhere else -- re-creates the dependency edge, and
    # with it the every-train destroy.
    droplet_refs = re.findall(r"digitalocean_droplet\.[A-Za-z0-9_-]+", fw)
    check(
        "database_firewall.pg references a droplet, so `-target=digitalocean_droplet.odoo` "
        "will destroy it again (GOL-2582)",
        not droplet_refs,
        f"found {sorted(set(droplet_refs))}",
    )

    # The droplet still has to be trusted, just not by id. Absent both a tag rule
    # and a droplet rule, the cluster is reachable only from the operator CIDRs
    # and QA Odoo cannot reach its own database -- so "no droplet ref" alone is
    # not a sufficient statement of intent.
    check(
        "database_firewall.pg has no `tag` rule -- the Odoo droplet would lose Postgres",
        re.search(r'type\s*=\s*"tag"', fw) is not None,
    )

    # Fail-closed tripwire: if the dependency ever comes back, the teardown must
    # ERROR rather than silently reopen the cluster.
    check(
        "database_firewall.pg is missing `prevent_destroy` -- a regression would "
        "reopen the cluster silently instead of failing the destroy",
        re.search(r"prevent_destroy\s*=\s*true", fw) is not None,
    )

    # The tag must actually be carried by the droplet, or the rule trusts nothing
    # and QA Odoo cannot connect. This is the half of the contract that lives on
    # the other resource.
    tag_block = extract_block(text, r'resource\s+"digitalocean_tag"\s+"pg_client"\s*')
    check("digitalocean_tag.pg_client is not declared", bool(tag_block))

    droplet = extract_block(text, r'resource\s+"digitalocean_droplet"\s+"odoo"\s*')
    check("digitalocean_droplet.odoo block not found", bool(droplet))
    check(
        "digitalocean_droplet.odoo does not carry digitalocean_tag.pg_client, so the "
        "tag rule would trust nothing and Odoo would lose its database",
        "digitalocean_tag.pg_client" in droplet,
    )

    # The teardown command this whole file is about. If the target ever stops
    # being the droplet the reasoning above needs re-reading, so pin it.
    teardown = (Path(__file__).resolve().parents[1] / "scripts/qa-l3-teardown.sh").read_text()
    check(
        "qa-l3-teardown.sh no longer targets digitalocean_droplet.odoo -- re-check "
        "whether this guard still describes the real blast radius",
        "-target=digitalocean_droplet.odoo" in teardown,
    )

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        print(f"\n{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("qa pg firewall independence: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
