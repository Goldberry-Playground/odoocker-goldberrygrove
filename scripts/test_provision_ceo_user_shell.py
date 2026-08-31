#!/usr/bin/env python3
"""Regression tests for provision_ceo_user_shell.py (GOL-1842).

No network, no real Odoo. A tiny in-memory fake supplies just enough of the
`env` surface for the pure resolver helpers so we can prove the properties that
actually broke on prod:

  groups-field-straddle   _groups_field() returns 'group_ids' on Odoo 19 (the
                          rename that raised `Invalid field 'groups_id'` and
                          aborted every live provision) and 'groups_id' on the
                          17/18 schema -- the version straddle must be resolved
                          at RUNTIME, never hardcoded either way.
  company-resolution      _resolve_companies() defaults to ALL companies with
                          the lowest-id company active, and honours an explicit
                          CEO_COMPANIES / CEO_MAIN_COMPANY override, always
                          keeping the active company inside the allowed set.

    python3 scripts/test_provision_ceo_user_shell.py

The module is loaded by exec'ing its source with the trailing shell-only
bootstrap (`raise SystemExit(main())`) stripped and a fake `env` injected, so
importing it here never tries to talk to a live Odoo shell.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "provision_ceo_user_shell.py")


# ── minimal fake Odoo recordset surface ───────────────────────────────────────

class _Company:
    def __init__(self, cid: int, name: str):
        self.id = cid
        self.name = name

    def __repr__(self):  # aids test failure output
        return f"_Company({self.id},{self.name!r})"


class _RS:
    """An ordered, de-duplicated recordset of _Company, with just the operators
    _resolve_companies() emits: search / browse / sorted / sudo / |= / iter /
    bool / membership."""

    def __init__(self, items=()):  # preserve order, drop dupes by id
        seen, out = set(), []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                out.append(it)
        self._items = out

    # env["res.company"].sudo() -> same recordset (sudo is a no-op here)
    def sudo(self):
        return self

    def browse(self):
        return _RS()

    def search(self, domain, order=None, limit=None):
        rows = list(self._items)
        for leaf in domain:
            field, op, val = leaf
            assert op == "=", f"fake only supports '='; got {op}"
            rows = [r for r in rows if getattr(r, field) == val]
        if order == "id":
            rows.sort(key=lambda r: r.id)
        if limit:
            rows = rows[:limit]
        return _RS(rows)

    def sorted(self, key):
        return _RS(sorted(self._items, key=lambda r: getattr(r, key)))

    @staticmethod
    def _members(other):  # accept an _RS or a bare _Company
        return list(other) if isinstance(other, _RS) else [other]

    def __ior__(self, other):  # allowed |= rec
        return _RS(self._items + self._members(other))

    def __or__(self, other):
        return _RS(self._items + self._members(other))

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __contains__(self, rec):  # active in allowed
        rec_ids = {m.id for m in self._members(rec)}
        return any(r.id in rec_ids for r in self._items)

    def __getitem__(self, i):  # Odoo: rs[0] is a size-1 recordset, not a bare record
        return _RS([self._items[i]])

    # Odoo proxies .id / .name on a singleton recordset onto its one record.
    @property
    def id(self):
        assert len(self._items) == 1, "id on non-singleton recordset"
        return self._items[0].id

    @property
    def name(self):
        assert len(self._items) == 1, "name on non-singleton recordset"
        return self._items[0].name


class _Model:
    def __init__(self, fields=None, rs=None):
        self._fields = fields or {}
        self._rs = rs if rs is not None else _RS()

    def sudo(self):
        return self._rs

    # a couple of models are reached as env["x"].search_count(...) directly
    def search_count(self, domain):
        return 0


class _FakeEnv:
    def __init__(self, companies, users_fields):
        self._models = {
            "res.company": _Model(rs=_RS(companies)),
            "res.users": _Model(fields=users_fields),
        }

    def __getitem__(self, name):
        return self._models[name]


def _load(users_fields, companies=()):
    """Exec the target module with the shell bootstrap stripped and a fake env."""
    src = open(_SRC, encoding="utf-8").read()
    src = src.replace("raise SystemExit(main())", "")
    ns = {"env": _FakeEnv(list(companies), users_fields), "__name__": "_prov_under_test"}
    exec(compile(src, _SRC, "exec"), ns)  # noqa: S102 - trusted local source
    return ns


# ── tests ─────────────────────────────────────────────────────────────────────

def test_groups_field_odoo19():
    ns = _load(users_fields={"group_ids": object(), "login": object()})
    got = ns["_groups_field"]()
    assert got == "group_ids", f"Odoo 19 must resolve group_ids, got {got!r}"


def test_groups_field_odoo17_18():
    ns = _load(users_fields={"groups_id": object(), "login": object()})
    got = ns["_groups_field"]()
    assert got == "groups_id", f"Odoo 17/18 must resolve groups_id, got {got!r}"


def test_companies_default_all_lowest_active():
    companies = [_Company(3, "GGG, LLC"), _Company(1, "Farm"), _Company(2, "Nursery")]
    ns = _load(users_fields={"group_ids": object()}, companies=companies)
    allowed, active = ns["_resolve_companies"](None)
    assert sorted(c.id for c in allowed) == [1, 2, 3], "default = ALL companies"
    assert active.id == 1, f"active = lowest-id company, got {active.id}"


def test_companies_explicit_override_active_forced_in():
    companies = [_Company(1, "Farm"), _Company(2, "At The Grove Nursery, LLC")]
    ns = _load(users_fields={"group_ids": object()}, companies=companies)
    # restrict allowed to Farm only, but make the (un-allowed) Nursery active:
    # the active company must be unioned back into the allowed set.
    ns["CEO_COMPANIES"] = "Farm"
    ns["CEO_MAIN_COMPANY"] = "At The Grove Nursery, LLC"
    allowed, active = ns["_resolve_companies"](None)
    assert active.name == "At The Grove Nursery, LLC"
    assert active in allowed, "active company must be forced into the allowed set"
    assert sorted(c.name for c in allowed) == ["At The Grove Nursery, LLC", "Farm"]


def _run():
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
