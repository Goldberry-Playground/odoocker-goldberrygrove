#!/usr/bin/env python3
"""Regression tests for rotate_disable_admin2_shell.py (GOL-2080).

No network, no real Odoo. A tiny in-memory fake supplies just enough of the
`env` surface (res.users browse/write/has_group, res.users.apikeys.search_count,
ir.cron.search, and a fake cursor for the password-column read) to prove the
properties that actually matter for a prod credential change:

  guard-ceo             refuse to touch uid=2 if its login IS the CEO login.
  guard-missing         refuse if uid=2 does not resolve to a row.
  report-readonly       MODE=report writes nothing and never commits.
  disable-blocks-on-dep  MODE=disable REFUSES (rc=4, no write) when any API key
                        or cron binds uid=2 -- archiving would break automation.
  disable-clean          MODE=disable with zero deps sets active=False AND
                        rotates the password, then commits once.
  rotate-keeps-active    MODE=rotate rotates the password but leaves active=True.
  password-never-emitted the rotated password is 32 chars from the intended
                        alphabet and is never returned/printed by the writer.

    python3 scripts/test_rotate_disable_admin2_shell.py

The module is loaded by exec'ing its source with the trailing shell-only
bootstrap (`raise SystemExit(main())`) stripped and a fake `env` injected, so
importing it here never talks to a live Odoo shell.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "rotate_disable_admin2_shell.py")


# -- minimal fake Odoo surface ------------------------------------------------

class _User:
    def __init__(self, uid=2, login="__system__@odoo", name="Administrator",
                 active=True, share=False, is_admin=True, exists=True):
        self.id = uid
        self.login = login
        self.name = name
        self.active = active
        self.share = share
        self._is_admin = is_admin
        self._exists = exists
        self.writes = []  # every write() payload, in order

    def exists(self):
        return self if self._exists else _User(exists=False)

    def __bool__(self):
        return self._exists

    def has_group(self, xmlid):
        return self._is_admin

    def write(self, vals):
        self.writes.append(dict(vals))
        if "active" in vals:
            self.active = vals["active"]
        return True


class _ApiKeys:
    def __init__(self, count):
        self._count = count

    def sudo(self):
        return self

    def search_count(self, domain):
        return self._count


class _Cron:
    def __init__(self, cid, name, active=True):
        self.id = cid
        self.name = name
        self.active = active


class _CronModel:
    def __init__(self, crons):
        self._crons = crons

    def sudo(self):
        return self

    def search(self, domain):
        return list(self._crons)


class _UsersModel:
    def __init__(self, user):
        self._user = user

    def sudo(self):
        return self

    def browse(self, uid):
        return self._user


class _Cursor:
    def __init__(self, pw_live):
        self._pw_live = pw_live
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self._last = (sql, params)

    def fetchone(self):
        # non-empty hash iff pw_live; None row if the user is missing
        return ("$hashed$value",) if self._pw_live else ("",)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Registry:
    def __init__(self, has_apikeys):
        self.models = {"res.users": 1, "ir.cron": 1}
        if has_apikeys:
            self.models["res.users.apikeys"] = 1


class _FakeEnv:
    def __init__(self, user, api_keys=0, crons=(), pw_live=True, has_apikeys=True):
        self._user = user
        self._models = {
            "res.users": _UsersModel(user),
            "res.users.apikeys": _ApiKeys(api_keys),
            "ir.cron": _CronModel(list(crons)),
        }
        self.cr = _Cursor(pw_live)
        self.registry = _Registry(has_apikeys)

    def __getitem__(self, name):
        return self._models[name]


def _load(mode="report", ceo_login=None, **env_kwargs):
    """Exec the target with the shell bootstrap stripped and a fake env + MODE."""
    os.environ["ADMIN2_MODE"] = mode
    if ceo_login is not None:
        os.environ["CEO_LOGIN"] = ceo_login
    else:
        os.environ.pop("CEO_LOGIN", None)
    src = open(_SRC, encoding="utf-8").read()
    src = src.replace("raise SystemExit(main())", "")
    env = _FakeEnv(**env_kwargs)
    ns = {"env": env, "__name__": "_admin2_under_test"}
    exec(compile(src, _SRC, "exec"), ns)  # noqa: S102 - trusted local source
    return ns, env


def _run(ns):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ns["main"]()
    return rc, buf.getvalue()


# -- tests --------------------------------------------------------------------

def test_guard_refuses_ceo_login():
    user = _User(login="josh@goldberrygrove.farm")
    ns, env = _load(mode="disable", user=user)
    rc, _out = _run(ns)
    assert rc == 3, f"must refuse the CEO login, got rc={rc}"
    assert user.writes == [], "must not write when refusing the CEO account"
    assert env.cr.commits == 0


def test_guard_refuses_missing_row():
    user = _User(exists=False)
    ns, env = _load(mode="disable", user=user)
    rc, _out = _run(ns)
    assert rc == 3, f"must refuse a missing uid=2 row, got rc={rc}"
    assert env.cr.commits == 0


def test_report_is_readonly():
    user = _User()
    ns, env = _load(mode="report", user=user, api_keys=0, crons=[])
    rc, out = _run(ns)
    assert rc == 0
    assert user.writes == [], "report must not write"
    assert env.cr.commits == 0, "report must not commit"
    assert "ADMIN2_REPORT" in out
    assert "recommended_action=disable" in out


def test_disable_blocks_on_apikey_dep():
    user = _User()
    ns, env = _load(mode="disable", user=user, api_keys=2, crons=[])
    rc, out = _run(ns)
    assert rc == 4, f"disable must refuse when API keys bind uid=2, got rc={rc}"
    assert user.writes == [], "must not archive when a dependency exists"
    assert env.cr.commits == 0
    assert "recommended_action=rotate" in out


def test_disable_blocks_on_cron_dep():
    user = _User()
    ns, env = _load(mode="disable", user=user, api_keys=0,
                    crons=[_Cron(9, "some scheduled action")])
    rc, _out = _run(ns)
    assert rc == 4, f"disable must refuse when a cron binds uid=2, got rc={rc}"
    assert user.writes == []


def test_disable_clean_archives_and_rotates():
    user = _User()
    ns, env = _load(mode="disable", user=user, api_keys=0, crons=[])
    rc, out = _run(ns)
    assert rc == 0
    assert len(user.writes) == 1, "exactly one write"
    vals = user.writes[0]
    assert vals.get("active") is False, "disable must archive"
    assert "password" in vals and isinstance(vals["password"], str)
    assert env.cr.commits == 1, "exactly one commit"
    assert "ADMIN2_RESULT" in out and "mode=disable" in out
    # the rotated secret must never be echoed
    assert vals["password"] not in out


def test_rotate_keeps_active():
    user = _User(active=True)
    ns, env = _load(mode="rotate", user=user, api_keys=3, crons=[_Cron(1, "x")])
    rc, out = _run(ns)
    assert rc == 0, "rotate is allowed even with dependencies"
    assert len(user.writes) == 1
    vals = user.writes[0]
    assert "active" not in vals, "rotate must NOT change active state"
    assert user.active is True
    assert "password" in vals
    assert env.cr.commits == 1
    assert vals["password"] not in out, "rotated secret must never be echoed"


def test_rotated_password_shape():
    user = _User()
    ns, _env = _load(mode="rotate", user=user, api_keys=0, crons=[])
    _run(ns)
    pw = user.writes[0]["password"]
    assert len(pw) == 32, f"expected 32-char password, got {len(pw)}"
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789!@#$%^&*-_")
    assert set(pw) <= allowed, "password uses only the intended alphabet"


def test_unknown_mode_rejected():
    user = _User()
    ns, env = _load(mode="frobnicate", user=user, api_keys=0, crons=[])
    rc, _out = _run(ns)
    assert rc == 2, f"unknown mode must exit 2, got rc={rc}"
    assert user.writes == []


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
