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

GOL-2581 (PR #753) fixed it by trusting the droplet through a dedicated TAG, so
the firewall depends on the tag -- a dependency of the droplet, not a dependent.
This file asserts that shape, plus the `prevent_destroy` tripwire that turns a
future regression into a failed destroy instead of a silently reopened cluster.

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

REPO = Path(__file__).resolve().parents[1]
MAIN_TF = REPO / "infra/terraform/environments/qa-app-platform/main.tf"
TEARDOWN_SH = REPO / "scripts/qa-l3-teardown.sh"


def strip_comments(text: str) -> str:
    """Blank out `#` / `//` / `/* */` comments, leaving code and line numbers.

    Every check below has to run on CODE, never on prose, and both directions
    bite. This block is heavily commented *about* the droplet by design, so a
    naive "is `digitalocean_droplet` mentioned?" scan fails on a correct config
    -- and, worse, the droplet block carries the comment
    `-- see digitalocean_tag.pg_client`, so a naive "is the tag carried?" scan
    PASSES on a config whose `tags =` line has been reverted. A guard that can
    be satisfied by a comment is not a guard.

    Quote-aware so a `#` inside a string is not mistaken for a comment.
    Replaces comment bytes with spaces rather than deleting them, so offsets
    (and the brace scan in extract_block) are unaffected.
    """
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


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


def code_lines(text: str) -> list[str]:
    """Lines with their leading-`#` comments dropped (shell flavour)."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def selftest() -> list[str]:
    """Prove strip_comments before trusting the checks that stand on it.

    Without this the guard's own blind spots are exactly as invisible as the
    bug it exists to catch.
    """
    bad: list[str] = []

    # A reference that only exists in prose must not read as a reference.
    commented = strip_comments('x {\n  # see digitalocean_droplet.odoo\n}\n')
    if "digitalocean_droplet" in commented:
        bad.append("selftest: strip_comments left a `#` comment's text behind")

    # ...and a commented-out setting must not read as set.
    if re.search(r"prevent_destroy\s*=\s*true", strip_comments("# prevent_destroy = true\n")):
        bad.append("selftest: a commented-out prevent_destroy still reads as set")

    # A `#` inside a string is data, not a comment (DO tags/CIDRs are quoted).
    kept = strip_comments('value = "a#b"\nname = "t"\n')
    if '"a#b"' not in kept or "name" not in kept:
        bad.append("selftest: strip_comments ate a `#` inside a string literal")

    # Code on the same line as a trailing comment survives.
    if 'type = "tag"' not in strip_comments('type = "tag" # why\n'):
        bad.append("selftest: strip_comments ate code before a trailing comment")

    # Brace offsets must be preserved for extract_block.
    src = 'resource "r" "n" {\n  # }\n  a = 1\n}\n'
    if extract_block(strip_comments(src), r'resource\s+"r"\s+"n"\s*').count("a = 1") != 1:
        bad.append("selftest: comment stripping broke the brace scan")

    return bad


def main() -> int:
    failures: list[str] = selftest()

    def check(label: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{label}{': ' + detail if detail else ''}")

    # Code only. See strip_comments: a comment must neither trip nor satisfy a
    # check, and this block argues about the droplet at length on purpose.
    text = strip_comments(MAIN_TF.read_text())

    fw = extract_block(text, r'resource\s+"digitalocean_database_firewall"\s+"pg"\s*')
    check("digitalocean_database_firewall.pg block not found", bool(fw))
    if not fw:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
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
    # Specifically on the `tags` argument: the reference has to be what grants
    # membership, not merely present somewhere in the block.
    check(
        "digitalocean_droplet.odoo does not carry digitalocean_tag.pg_client on its "
        "`tags`, so the tag rule would trust nothing and Odoo would lose its database",
        re.search(r"^\s*tags\s*=.*digitalocean_tag\.pg_client", droplet, re.M) is not None,
    )

    # The teardown command this whole file is about. If the target ever stops
    # being the droplet the reasoning above needs re-reading, so pin it -- on a
    # live line, not on the comment in that script that quotes the same string.
    teardown = code_lines(TEARDOWN_SH.read_text())
    check(
        "qa-l3-teardown.sh no longer targets digitalocean_droplet.odoo on a live line "
        "-- re-check whether this guard still describes the real blast radius",
        any("-target=digitalocean_droplet.odoo" in ln for ln in teardown),
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
