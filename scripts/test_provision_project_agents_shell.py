#!/usr/bin/env python3
"""Regression tests for provision_project_agents_shell.py (GOL-2095).

No network, no real Odoo. A tiny in-memory fake supplies just enough of the
`env` surface for the pure resolver helpers, so we can prove the properties that
matter for a per-agent, least-privilege Projects roster:

  groups-field-straddle   _groups_field() returns 'group_ids' on Odoo 19 (the
                          rename that raised `Invalid field 'groups_id'` on the
                          CEO/logistics scripts) and 'groups_id' on 17/18.
  least-privilege groups  _resolve_group_ids('user') = base.group_user +
                          project.group_project_user ONLY; 'manager' ADDS
                          project.group_project_manager. Accounting/Settings
                          xmlids are never requested.
  required-group-abort    a missing REQUIRED group (project app absent) is
                          reported so the caller aborts instead of creating an
                          under-scoped or over-scoped user.
  roster-filter           _effective_roster() honours AGENT_LOGIN and SKIPS
                          humans that have no login/email yet (safe to run for
                          agents before human emails are supplied).
  portal-disjoint         _exclusive_group_removals() drops portal/public so an
                          internal-group promotion doesn't trip _check_disjoint.
  company-resolution      _resolve_companies() defaults to ALL companies with
                          the lowest-id company active.

    python3 scripts/test_provision_project_agents_shell.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "provision_project_agents_shell.py")


# ── minimal fake Odoo recordset surface (shared shape with the CEO test) ──────

class _Company:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name

    def __repr__(self):
        return f"_Company({self.id},{self.name!r})"


class _RS:
    def __init__(self, items=()):
        seen, out = set(), []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                out.append(it)
        self._items = out

    def sudo(self):
        return self

    def browse(self):
        return _RS()

    def search(self, domain, order=None, limit=None):
        rows = list(self._items)
        for field, op, val in domain:
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
    def _members(other):
        return list(other) if isinstance(other, _RS) else [other]

    def __ior__(self, other):
        return _RS(self._items + self._members(other))

    def __or__(self, other):
        return _RS(self._items + self._members(other))

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __contains__(self, rec):
        rec_ids = {m.id for m in self._members(rec)}
        return any(r.id in rec_ids for r in self._items)

    def __getitem__(self, i):
        return _RS([self._items[i]])

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

    def search(self, domain, order=None, limit=None):
        return self._rs.search(domain, order=order, limit=limit)


class _Grp:
    def __init__(self, gid):
        self.id = gid
        self._name = "res.groups"


class _User:
    def __init__(self, groups_field, group_ids):
        setattr(self, groups_field, type("_M2M", (), {"ids": list(group_ids)})())


# res.groups xmlid -> id. The roster's project groups plus the exclusive roles.
_GROUP_XMLIDS = {
    "base.group_user": 1,
    "project.group_project_user": 20,
    "project.group_project_manager": 21,
    "base.group_portal": 10,
    "base.group_public": 11,
}


class _FakeEnv:
    def __init__(self, companies, users_fields, known_groups):
        self._known = known_groups
        self._models = {
            "res.company": _Model(rs=_RS(companies)),
            "res.users": _Model(fields=users_fields, rs=_RS()),
        }

    def __getitem__(self, name):
        return self._models[name]

    def ref(self, xmlid, raise_if_not_found=True):
        gid = self._known.get(xmlid)
        if gid is None:
            if raise_if_not_found:
                raise ValueError(xmlid)
            return None
        return _Grp(gid)


def _load(users_fields, companies=(), known_groups=None):
    """Exec the target module with the shell bootstrap stripped and a fake env."""
    if known_groups is None:
        known_groups = _GROUP_XMLIDS
    src = open(_SRC, encoding="utf-8").read()
    src = src.replace("raise SystemExit(main())", "")
    ns = {"env": _FakeEnv(list(companies), users_fields, known_groups),
          "__name__": "_prov_agents_under_test"}
    exec(compile(src, _SRC, "exec"), ns)  # noqa: S102 - trusted local source
    return ns


# ── tests ─────────────────────────────────────────────────────────────────────

def test_groups_field_odoo19():
    ns = _load(users_fields={"group_ids": object(), "login": object()})
    assert ns["_groups_field"]() == "group_ids"


def test_groups_field_odoo17_18():
    ns = _load(users_fields={"groups_id": object(), "login": object()})
    assert ns["_groups_field"]() == "groups_id"


def test_user_tier_is_least_privilege():
    ns = _load(users_fields={"group_ids": object()})
    ids, missing = ns["_resolve_group_ids"]("user")
    assert not missing, missing
    assert set(ids) == {_GROUP_XMLIDS["base.group_user"],
                        _GROUP_XMLIDS["project.group_project_user"]}, ids


def test_manager_tier_adds_project_manager_only():
    ns = _load(users_fields={"group_ids": object()})
    ids, missing = ns["_resolve_group_ids"]("manager")
    assert not missing, missing
    assert set(ids) == {_GROUP_XMLIDS["base.group_user"],
                        _GROUP_XMLIDS["project.group_project_user"],
                        _GROUP_XMLIDS["project.group_project_manager"]}, ids


def test_no_accounting_or_settings_group_ever_requested():
    # The static xmlid lists must never mention accounting/settings/admin.
    src = open(_SRC, encoding="utf-8").read()
    for banned in ("account.group_account", "base.group_system",
                   "base.group_erp_manager", "stock.group_stock_manager",
                   "purchase.group_purchase", "sales_team.group_sale_manager"):
        # allowed only inside prose comments listing what is NOT granted; assert
        # it never appears in a REQUIRED/MANAGER xmlid list line.
        for line in src.splitlines():
            s = line.strip()
            if s.startswith('"') and banned in s:
                raise AssertionError(f"banned group in an xmlid list line: {line}")


def test_required_group_missing_aborts():
    # project app absent -> project.group_project_user unresolvable -> reported
    # as missing_required so the caller aborts (no under-scoped user created).
    ns = _load(users_fields={"group_ids": object()},
               known_groups={"base.group_user": 1})
    ids, missing = ns["_resolve_group_ids"]("user")
    assert "project.group_project_user" in missing, missing


def test_manager_group_missing_degrades_not_aborts():
    # manager group absent -> WARN, not a required-missing abort (degrades to user).
    ns = _load(users_fields={"group_ids": object()},
               known_groups={"base.group_user": 1, "project.group_project_user": 20})
    ids, missing = ns["_resolve_group_ids"]("manager")
    assert missing == [], missing
    assert _GROUP_XMLIDS["project.group_project_user"] in ids


def test_roster_filter_single_login():
    ns = _load(users_fields={"group_ids": object()})
    ns["ONLY_LOGIN"] = "agent-ada"
    roster = ns["_effective_roster"]()
    assert [m["login"] for m in roster] == ["agent-ada"], roster


def test_humans_without_email_are_skipped():
    ns = _load(users_fields={"group_ids": object()})
    ns["ONLY_LOGIN"] = ""
    roster = ns["_effective_roster"]()
    kinds = {m["kind"] for m in roster}
    assert kinds == {"agent"}, f"humans with no email must be skipped, got {kinds}"
    # all six agents present
    assert len([m for m in roster if m["kind"] == "agent"]) == 6, roster


def test_human_with_email_is_included():
    ns = _load(users_fields={"group_ids": object()})
    ns["ONLY_LOGIN"] = ""
    # Simulate George's email being filled in.
    for m in ns["ROSTER"]:
        if m["name"] == "George":
            m["email"] = "george@goldberrygrove.farm"
    roster = ns["_effective_roster"]()
    george = [m for m in roster if m["name"] == "George"]
    assert george and george[0]["login"] == "george@goldberrygrove.farm", roster


def test_portal_user_drops_exclusive_group():
    ns = _load(users_fields={"group_ids": object()})
    user = _User("group_ids", [_GROUP_XMLIDS["base.group_portal"]])
    removals = ns["_exclusive_group_removals"](user, "group_ids")
    assert removals == [(3, _GROUP_XMLIDS["base.group_portal"])], removals


def test_internal_user_no_removals():
    ns = _load(users_fields={"group_ids": object()})
    user = _User("group_ids", [_GROUP_XMLIDS["base.group_user"]])
    assert ns["_exclusive_group_removals"](user, "group_ids") == []


def test_companies_default_all_lowest_active():
    companies = [_Company(3, "GGG, LLC"), _Company(1, "Farm"), _Company(2, "Nursery")]
    ns = _load(users_fields={"group_ids": object()}, companies=companies)
    allowed, active = ns["_resolve_companies"]()
    assert sorted(c.id for c in allowed) == [1, 2, 3], "default = ALL companies"
    assert active.id == 1, f"active = lowest-id company, got {active.id}"


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
