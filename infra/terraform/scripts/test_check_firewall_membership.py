#!/usr/bin/env python3
"""Regression tests for the firewall-membership guard's HCL parsing (no network).

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


if __name__ == "__main__":
    test_droplet_ids_survives_an_index_bracket()
    test_managed_ref_does_not_swallow_the_data_form()
    test_literal_name_falls_back_to_a_variable_default()
    test_parse_env_resolves_both_firewall_shapes()
    test_iter_blocks_does_not_run_past_a_nested_block()
    print("ok: firewall-membership parser regression tests", file=sys.stderr)
