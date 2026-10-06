#!/usr/bin/env python3
"""Regression tests for scripts/prod_dedupe_ir_filters.py (GOL-3130).

No network, no Odoo. A fake client models just enough of `execute_kw`
(`ir.module.module`, `ir.model.data`, `ir.filters`) to drive every gate, so the
properties under test are the ones that would actually damage prod:

  refuses-before-promote      grove_headless 19.0.1.55.5 (what prod runs
                              today) => exit 4 and NO unlink. Deleting the
                              hand-built rows pre-promote leaves prod with no
                              balance-not-charged filters at all, which is the
                              one irreversible-feeling mistake available here.
  refuses-module-not-installed  a module in state 'to upgrade' is not a
                              promoted module.
  refuses-missing-xmlids      right version, but the module's own rows are
                              absent => exit 5 rather than deleting the only
                              copies that exist.
  refuses-severed-xmlid       an xml-id pointing at a dead ir.filters id is a
                              GOL-2134-shaped severance, not a duplicate.
  refuses-xmlid-candidate     never unlink a row that owns module data (exit 6).
  refuses-unexpected-id       a 4th same-named hand row outside {9,10,11}
                              stops the run (exit 7) instead of guessing.
  dry-run-writes-nothing      the default mode plans the delete and issues no
                              unlink call.
  deletes-only-the-duplicates the unlink carries exactly [9, 10, 11] -- never
                              the module's keep-ids.
  idempotent-rerun            post-cleanup the script reports converged and
                              issues no unlink.
  post-assert-catches-residue a delete that leaves two rows per name fails
                              (exit 8) instead of reporting success.
  post-assert-needs-xmlid     a surviving row with no grove_headless
                              ir.model.data entry fails the success condition.
  version-compare-pads        "19.0" never reads as >= "19.0.1.66.0".
  xml-matches-shipped-data    EXPECTED_FILTERS matches the real shipped
                              grove_zone_filters.xml when it is available, so a
                              rename on either side cannot silently drift.

    python3 scripts/test_prod_dedupe_ir_filters.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import prod_dedupe_ir_filters as dd  # noqa: E402

GOOD_VERSION = dd.MIN_MODULE_VERSION
PROD_TODAY = "19.0.1.55.5"

XMLIDS = sorted(dd.EXPECTED_FILTERS)
NAMES = [dd.EXPECTED_FILTERS[x] for x in XMLIDS]

# The shape prod will be in right after the Train #3 promote: module rows at
# 12/13/14, Josh's hand-built originals still at 9/10/11.
KEEP_IDS = [12, 13, 14]
LEGACY_IDS = [9, 10, 11]


class FakeClient:
    """Records every call; answers the three models the script reads."""

    def __init__(self, *, version=GOOD_VERSION, state="installed", module_data=None,
                 filters=None):
        self.version = version
        self.state = state
        self.module_data = module_data if module_data is not None else []
        self.filters = filters if filters is not None else []
        self.calls: list[tuple] = []
        self.uid, self.db, self.url = 8, "odoo", "https://odoo.example.invalid"

    # -- helpers -----------------------------------------------------------
    @property
    def unlinked(self):
        return [c[2][0] for c in self.calls if c[:2] == ("ir.filters", "unlink")]

    def _domain(self, args):
        return args[0] if args else []

    @staticmethod
    def _match(rec, domain):
        for field, op, val in domain:
            got = rec.get(field)
            if op == "=" and got != val:
                return False
            if op == "in" and got not in val:
                return False
        return True

    # -- transport ---------------------------------------------------------
    def call(self, model, method, args=None, kwargs=None):
        args, kwargs = list(args or []), kwargs or {}
        self.calls.append((model, method, args, kwargs))

        if model == "ir.module.module":
            rows = [{"id": 83, "name": dd.MODULE, "state": self.state,
                     "installed_version": self.version}]
            return [r for r in rows if self._match(r, self._domain(args))]

        if model == "ir.model.data":
            return [r for r in self.module_data if self._match(r, self._domain(args))]

        if model == "ir.filters":
            if method == "unlink":
                ids = set(args[0])
                self.filters = [f for f in self.filters if f["id"] not in ids]
                self.module_data = [d for d in self.module_data if d["res_id"] not in ids]
                return True
            return [f for f in self.filters if self._match(f, self._domain(args))]

        raise AssertionError(f"unexpected model {model!r}")


def _filters(ids_names):
    return [{"id": i, "name": n, "model_id": dd.MODEL, "user_ids": [], "active": True}
            for i, n in ids_names]


def _module_data(pairs):
    return [{"id": 900 + k, "model": "ir.filters", "module": dd.MODULE,
             "name": x, "res_id": i} for k, (x, i) in enumerate(pairs)]


def promoted_with_duplicates() -> FakeClient:
    """Prod immediately after the promote: each filter present twice."""
    return FakeClient(
        module_data=_module_data(zip(XMLIDS, KEEP_IDS)),
        filters=_filters(list(zip(LEGACY_IDS, NAMES)) + list(zip(KEEP_IDS, NAMES))),
    )


def converged() -> FakeClient:
    """Prod after a successful cleanup: only the module rows remain."""
    return FakeClient(
        module_data=_module_data(zip(XMLIDS, KEEP_IDS)),
        filters=_filters(list(zip(KEEP_IDS, NAMES))),
    )


def _refused(client):
    try:
        dd.plan_deletion(client)
    except dd.Refused as exc:
        return exc
    raise AssertionError("expected Refused, got a plan")


# --- the gates --------------------------------------------------------------


def test_refuses_before_promote():
    c = promoted_with_duplicates()
    c.version = PROD_TODAY  # what prod actually runs on 2026-10-06
    exc = _refused(c)
    assert exc.code == 4, f"expected exit 4, got {exc.code}"
    assert PROD_TODAY in str(exc), str(exc)
    assert not c.unlinked, "refused run must not unlink"


def test_refuses_module_not_installed():
    c = promoted_with_duplicates()
    c.state = "to upgrade"
    assert _refused(c).code == 4


def test_refuses_missing_xmlids():
    c = FakeClient(module_data=[], filters=_filters(zip(LEGACY_IDS, NAMES)))
    exc = _refused(c)
    assert exc.code == 5, f"expected exit 5, got {exc.code}"
    assert not c.unlinked


def test_refuses_severed_xmlid():
    # xml-ids recorded, but the rows they point at are gone (GOL-2134 shape).
    c = FakeClient(
        module_data=_module_data(zip(XMLIDS, KEEP_IDS)),
        filters=_filters(zip(LEGACY_IDS, NAMES)),
    )
    exc = _refused(c)
    assert exc.code == 5, f"expected exit 5, got {exc.code}"
    assert "severed" in str(exc), str(exc)


def test_refuses_xmlid_candidate():
    c = promoted_with_duplicates()
    # some other module adopted id 9
    c.module_data.append({"id": 999, "model": "ir.filters", "module": "other_mod",
                          "name": "adopted", "res_id": 9})
    exc = _refused(c)
    assert exc.code == 6, f"expected exit 6, got {exc.code}"
    assert not c.unlinked


def test_refuses_unexpected_id():
    c = promoted_with_duplicates()
    c.filters += _filters([(42, NAMES[0])])  # a 4th hand-built row
    exc = _refused(c)
    assert exc.code == 7, f"expected exit 7, got {exc.code}"
    assert "42" in str(exc), str(exc)
    assert not c.unlinked


# --- the happy paths --------------------------------------------------------


def test_dry_run_writes_nothing():
    c = promoted_with_duplicates()
    plan = dd.plan_deletion(c)
    assert plan["delete"] == LEGACY_IDS, plan["delete"]
    assert not c.unlinked, "plan_deletion must be read-only"


def test_deletes_only_the_duplicates():
    c = promoted_with_duplicates()
    plan = dd.plan_deletion(c)
    c.call("ir.filters", "unlink", [plan["delete"]])
    assert c.unlinked == [LEGACY_IDS], c.unlinked
    for kept in KEEP_IDS:
        assert kept not in plan["delete"], f"would have deleted module row {kept}"
    state = dd.assert_converged(c)
    assert state["problems"] == [], state["problems"]


def test_idempotent_rerun():
    c = converged()
    plan = dd.plan_deletion(c)
    assert plan["delete"] == [], plan["delete"]
    assert dd.assert_converged(c)["problems"] == []
    assert not c.unlinked


# --- the success condition --------------------------------------------------


def test_post_assert_catches_residue():
    c = promoted_with_duplicates()  # duplicates still present
    problems = dd.assert_converged(c)["problems"]
    # Both halves of the success condition fail pre-cleanup: two rows per name,
    # and the extra (hand-built) row carries no xml-id.
    counts = [p for p in problems if "expected exactly 1" in p]
    unstamped = [p for p in problems if "ir.model.data" in p]
    assert len(counts) == 3, counts
    assert len(unstamped) == 3, unstamped
    assert all(f"id {i}" in " ".join(unstamped) for i in LEGACY_IDS), unstamped


def test_post_assert_needs_xmlid():
    # one row per name, but the survivors are the UNSTAMPED hand-built ones
    c = FakeClient(module_data=[], filters=_filters(zip(LEGACY_IDS, NAMES)))
    problems = dd.assert_converged(c)["problems"]
    assert len(problems) == 3, problems
    assert all("ir.model.data" in p for p in problems), problems


def test_version_compare_pads():
    assert not dd.version_at_least("19.0", dd.MIN_MODULE_VERSION)
    assert not dd.version_at_least(PROD_TODAY, dd.MIN_MODULE_VERSION)
    assert not dd.version_at_least("", dd.MIN_MODULE_VERSION)
    assert not dd.version_at_least("garbage", dd.MIN_MODULE_VERSION)
    assert dd.version_at_least(dd.MIN_MODULE_VERSION, dd.MIN_MODULE_VERSION)
    assert dd.version_at_least("19.0.1.66", dd.MIN_MODULE_VERSION)
    assert dd.version_at_least("19.0.2.0.0", dd.MIN_MODULE_VERSION)


def test_xml_matches_shipped_data():
    """Keep EXPECTED_FILTERS honest against grove_zone_filters.xml if present.

    grove-odoo-modules is a separate repo, so this is a soft check: it asserts
    only when the file can be found via GROVE_MODULES_DIR or a sibling clone.
    """
    import xml.etree.ElementTree as ET

    roots = [os.environ.get("GROVE_MODULES_DIR"),
             os.path.join(os.path.dirname(_HERE), "..", "grove-odoo-modules")]
    path = None
    for r in roots:
        if not r:
            continue
        cand = os.path.join(r, "grove_headless", "data", "grove_zone_filters.xml")
        if os.path.exists(cand):
            path = cand
            break
    if path is None:
        print("     (skipped: grove_zone_filters.xml not available locally)")
        return

    shipped = {}
    for rec in ET.parse(path).getroot().iter("record"):
        if rec.get("model") != "ir.filters":
            continue
        name = rec.find("./field[@name='name']")
        shipped[rec.get("id")] = (name.text or "").strip() if name is not None else ""
    assert shipped == dd.EXPECTED_FILTERS, (
        f"drifted from {path}:\n  shipped={shipped}\n  script ={dd.EXPECTED_FILTERS}"
    )


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
