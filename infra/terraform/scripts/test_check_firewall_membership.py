#!/usr/bin/env python3
"""Regression tests for the firewall-membership guard's parsing + fetch layer (no network).

Run: python3 infra/terraform/scripts/test_check_firewall_membership.py

Every case here is a shape that once made the guard pass something it should
have watched. GOL-2565's whole lesson is that a firewall check which "passes"
for the wrong reason is worse than no check, so the parser's silent-skip paths
are what these pin down.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import urllib.error
from pathlib import Path

_SRC = Path(__file__).with_name("check-firewall-membership.py")
_spec = importlib.util.spec_from_file_location("fwcheck", _SRC)
assert _spec and _spec.loader
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)


def test_droplet_ids_survives_an_index_bracket() -> None:
    """The bug: non-greedy `.*?\\]` stopped at `[0]` and truncated the ref.

    `[data.digitalocean_droplet.x[0].id]` lost its `.id`, so no ref matched and
    the whole firewall became `SKIP: declares no droplet_ids` -- exit 0.
    """
    body = "  droplet_ids = [data.digitalocean_droplet.agent_plane[0].id]\n"
    inner = fw.DROPLET_IDS.search(body).group(1)
    assert inner == "data.digitalocean_droplet.agent_plane[0].id", inner
    assert fw.DATA_DROPLET_REF.findall(inner) == ["agent_plane"]

    # a multi-element list with mixed forms, and `[count.index]` not just `[0]`
    body = (
        "  droplet_ids = [\n"
        "    digitalocean_droplet.odoo.id,\n"
        "    data.digitalocean_droplet.legacy_ghost[count.index].id,\n"
        "  ]\n"
    )
    inner = fw.DROPLET_IDS.search(body).group(1)
    assert fw.DROPLET_REF.findall(inner) == ["odoo"], fw.DROPLET_REF.findall(inner)
    assert fw.DATA_DROPLET_REF.findall(inner) == ["legacy_ghost"]


def test_managed_ref_does_not_swallow_the_data_form() -> None:
    """`(?<!\\.)` guard: a data-source ref must NOT register as a managed droplet.

    Without it the parser invents `digitalocean_droplet.legacy_ghost`, which no
    resource block defines, and the firewall reports "interpolated/absent name"
    -- another silent skip.
    """
    inner = "data.digitalocean_droplet.legacy_ghost[0].id"
    assert fw.DROPLET_REF.findall(inner) == []
    assert fw.DATA_DROPLET_REF.findall(inner) == ["legacy_ghost"]
    # ...while the plain managed form still matches
    assert fw.DROPLET_REF.findall("digitalocean_droplet.odoo.id") == ["odoo"]


def test_literal_name_falls_back_to_a_variable_default() -> None:
    assert fw.literal_name('  name = "grove-prod-odoo"\n') == "grove-prod-odoo"
    # `name = var.x` is unresolvable without the env's variable defaults
    body = "  count = var.gate ? 1 : 0\n  name  = var.legacy_ghost_droplet_name\n"
    assert fw.literal_name(body) is None
    assert (
        fw.literal_name(body, {"legacy_ghost_droplet_name": "ghostgoldberrygrove-nyc1"})
        == "ghostgoldberrygrove-nyc1"
    )
    # an interpolated name stays unresolved rather than being guessed
    assert fw.literal_name('  name = "${local.name}-fw"\n', {"x": "y"}) is None


def _write_env(d: Path) -> None:
    (d / "variables.tf").write_text(
        'variable "gate" {\n  type    = bool\n  default = false\n}\n\n'
        'variable "boxname" {\n  type    = string\n  default = "ghostgoldberrygrove-nyc1"\n}\n'
    )
    (d / "main.tf").write_text(
        'resource "digitalocean_droplet" "odoo" {\n  name = "grove-prod-odoo"\n}\n\n'
        'resource "digitalocean_firewall" "odoo" {\n'
        '  name        = "grove-prod-odoo-fw"\n'
        "  droplet_ids = [digitalocean_droplet.odoo.id]\n"
        '  inbound_rule {\n    protocol = "tcp"\n    port_range = "22"\n  }\n'
        "}\n\n"
        'data "digitalocean_droplet" "legacy" {\n'
        "  count = var.gate ? 1 : 0\n  name  = var.boxname\n}\n\n"
        'resource "digitalocean_firewall" "legacy" {\n'
        "  count       = var.gate ? 1 : 0\n"
        '  name        = "grove-legacy-ghost-fw"\n'
        "  droplet_ids = [data.digitalocean_droplet.legacy[0].id]\n"
        "}\n"
    )


def test_parse_env_resolves_both_firewall_shapes() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write_env(d)
        droplets, firewalls = fw.parse_env(d)

    assert droplets["digitalocean_droplet.odoo"] == "grove-prod-odoo"
    # the data-source droplet is keyed by its `data.` address and its name comes
    # from the variable default -- this is the un-codified snowflake path
    assert droplets["data.digitalocean_droplet.legacy"] == "ghostgoldberrygrove-nyc1"

    managed = firewalls["digitalocean_firewall.odoo"]
    assert managed["fw_name"] == "grove-prod-odoo-fw"
    assert managed["refs"] == ["digitalocean_droplet.odoo"]
    # no `count` on the managed firewall -> its absence live is real drift
    assert managed["count"] is None

    gated = firewalls["digitalocean_firewall.legacy"]
    assert gated["fw_name"] == "grove-legacy-ghost-fw"
    assert gated["refs"] == ["data.digitalocean_droplet.legacy"], gated["refs"]
    # `count` present -> absence live is an expected SKIP, not drift, because the
    # holding-action fences default their gate false so merging is not an apply
    assert gated["count"] == "var.gate ? 1 : 0", gated["count"]


def test_iter_blocks_does_not_run_past_a_nested_block() -> None:
    text = (
        'resource "digitalocean_firewall" "a" {\n'
        '  name = "fw-a"\n'
        '  inbound_rule {\n    protocol = "tcp"\n  }\n'
        "}\n"
        'resource "digitalocean_firewall" "b" {\n  name = "fw-b"\n}\n'
    )
    tops = [h for h, _ in fw.iter_blocks(text) if h.startswith("resource")]
    assert tops == [
        'resource "digitalocean_firewall" "a"',
        'resource "digitalocean_firewall" "b"',
    ], tops
    bodies = {h: b for h, b in fw.iter_blocks(text)}
    assert fw.literal_name(bodies['resource "digitalocean_firewall" "a"']) == "fw-a"



# ---------------------------------------------------------------------------
# The managed-database fetch layer (GOL-2582 / GOL-2913). `census()` itself is
# pinned by the script's own `--selftest`, which can stay offline because the
# fetch lives out here. THESE are the two decisions that cannot be made inside
# census(): what a per-cluster read failure returns, and what losing the whole
# cluster list does. Both are "how the check behaves when the API says no",
# which is precisely where a security check quietly turns into a no-op.
# ---------------------------------------------------------------------------

_PROD_RULES = [
    {"type": "ip_addr", "value": "173.84.140.152"},
    {"type": "droplet", "value": "601081550"},
]


def _patch_api(fn):
    """Swap `fw.api` for `fn` and give back a restore callable."""
    saved = fw.api
    fw.api = fn
    return lambda: setattr(fw, "api", saved)


def test_a_per_cluster_read_failure_is_none_not_empty() -> None:
    """`None` and `[]` are different claims and must not collapse.

    `[]` is "read fine, the cluster narrows nothing" -- the GOL-2582 exposure.
    `None` is "could not read it". Returning `[]` for a failed read would
    INVENT that exposure; returning nothing at all would hide a real one by
    dropping the cluster from the census entirely. So the cluster is still
    returned, paired with None.
    """

    def api(path, token):
        if path.startswith("databases?"):
            return {"databases": [{"id": "c-ok", "name": "a"}, {"id": "c-bad", "name": "b"}]}
        if path == "databases/c-ok/firewall":
            return {"rules": _PROD_RULES}
        raise urllib.error.HTTPError(path, 403, "forbidden", None, None)

    restore = _patch_api(api)
    try:
        pairs, fatal = fw.fetch_database_firewalls("tok")
    finally:
        restore()

    assert fatal is None, fatal
    assert [c["id"] for c, _ in pairs] == ["c-ok", "c-bad"], pairs
    assert pairs[0][1] == _PROD_RULES
    assert pairs[1][1] is None, "a failed read must be None, never []"


def test_a_missing_rules_key_is_empty_not_none() -> None:
    """`{}` from the API is still a successful read of nothing.

    Only an exception means "could not read". A response without a `rules` key
    must land as `[]` -- i.e. as the EMPTY-trusted-sources finding -- and not be
    laundered into the softer UNREADABLE one.
    """

    def api(path, token):
        if path.startswith("databases?"):
            return {"databases": [{"id": "c", "name": "a"}]}
        return {}

    restore = _patch_api(api)
    try:
        pairs, fatal = fw.fetch_database_firewalls("tok")
    finally:
        restore()
    assert fatal is None
    assert pairs[0][1] == [], pairs


def test_losing_the_cluster_list_is_fatal_and_returns_no_pairs() -> None:
    """A census that cannot ask must not print "0 database cluster(s)".

    That line is indistinguishable from an account with no clusters, which is
    the exact silence this leg exists to remove -- so the caller turns this
    into exit 2 (the workflow's "the watcher could not run" alert) rather than
    a quiet omission. The empty pair list is what stops a caller that ignored
    the error from reporting a clean bill of health.
    """

    def api(path, token):
        raise urllib.error.URLError("no route to host")

    restore = _patch_api(api)
    try:
        pairs, fatal = fw.fetch_database_firewalls("tok")
    finally:
        restore()
    assert pairs == [], pairs
    assert fatal and "database clusters" in fatal, fatal


def test_an_account_with_no_clusters_is_not_an_error() -> None:
    def api(path, token):
        return {"databases": []}

    restore = _patch_api(api)
    try:
        pairs, fatal = fw.fetch_database_firewalls("tok")
    finally:
        restore()
    assert (pairs, fatal) == ([], None)


def test_world_db_covers_the_maskless_forms() -> None:
    """A trusted-source `ip_addr` rule holds a BARE address, so the maskless
    forms are the ones an operator would actually type. Matching only the
    droplet-side CIDR set would wave them through."""
    assert fw.WORLD <= fw.WORLD_DB
    for value in ("0.0.0.0", "::", "0.0.0.0/0", "::/0"):
        assert value in fw.WORLD_DB, value
    # ...and it must not have grown into a blanket match on anything short.
    for value in ("10.0.0.0", "0.0.0.1", "173.84.140.152", ""):
        assert value not in fw.WORLD_DB, value


if __name__ == "__main__":
    test_droplet_ids_survives_an_index_bracket()
    test_managed_ref_does_not_swallow_the_data_form()
    test_literal_name_falls_back_to_a_variable_default()
    test_parse_env_resolves_both_firewall_shapes()
    test_iter_blocks_does_not_run_past_a_nested_block()
    test_a_per_cluster_read_failure_is_none_not_empty()
    test_a_missing_rules_key_is_empty_not_none()
    test_losing_the_cluster_list_is_fatal_and_returns_no_pairs()
    test_an_account_with_no_clusters_is_not_an_error()
    test_world_db_covers_the_maskless_forms()
    print("ok: firewall-membership parser + fetch regression tests", file=sys.stderr)
